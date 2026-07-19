"""Control-plane command routing shared by the gRPC servicer and the Port.

The control plane sends an ``ApiCommand`` on a Worker's live outbound stream and
awaits the correlated ``CommandResult`` (CONTROL_PLANE.md Sections 3, 5). Two
pieces of mutable state make that possible and are shared between the servicer
(which owns the streams) and the :class:`ControlPlane` adapter (which callers
use):

- **per-worker outbound queues** — one ``asyncio.Queue`` per *current* session;
  the servicer's ``Session`` generator drains its queue and yields each
  ``ApiMessage`` onto that Worker's stream. A reconnect replaces the queue, so a
  command always rides the live session.
- **a pending-correlation map** — ``command_id`` -> ``Future``; the servicer
  resolves the future when the matching ``CommandResult`` arrives on the inbound
  stream, unblocking :meth:`GrpcControlPlane.dispatch`.

Only this adapter and the servicer touch grpcio / the generated stubs; the
domain Port stays transport-free (ARCHITECTURE.md Section 2.1).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from google.protobuf.timestamp_pb2 import Timestamp

from mc_server_dashboard_api.fleet.domain.control_plane import (
    CloseBedrockTunnelCommand,
    Command,
    CommandResult,
    CommandResultCode,
    CommandTimedOutError,
    ControlPlane,
    EditFileCommand,
    FileAccessReason,
    FileEntry,
    FileListing,
    HydrateCommand,
    LaunchMode,
    ListFilesCommand,
    OpenBedrockTunnelCommand,
    ReadFileCommand,
    RestartServerCommand,
    ServerCommandCommand,
    SnapshotCommand,
    StartServerCommand,
    StopServerCommand,
    TunnelDialCommand,
    WorkerNotConnectedError,
)
from mc_server_dashboard_api.fleet.domain.late_snapshot_sink import (
    LateSnapshotResultSink,
)
from mc_server_dashboard_api.fleet.domain.registry import SessionToken
from mc_server_dashboard_api.fleet.domain.value_objects import DriverKind, WorkerId
from mcsd.controlplane.v1 import control_plane_pb2 as pb

_LOG = logging.getLogger(__name__)


class StaleSessionError(RuntimeError):
    """Raised when ``open_session`` receives a token older than the latest opened.

    Defense-in-depth against the reconnect race (issue #1694): a stale session
    that resumes its DB awaits after a newer session already opened must not
    overwrite the current outbound queue.
    """

    def __init__(self, worker_id: str, session: int) -> None:
        super().__init__(
            f"worker {worker_id}: session {session} is stale "
            "(a newer session was already opened)"
        )
        self.worker_id = worker_id
        self.session = session


def _now_timestamp() -> Timestamp:
    """Return a protobuf Timestamp set to the current wall-clock time."""
    ts = Timestamp()
    ts.GetCurrentTime()
    return ts


# Map the domain driver kind to the wire enum at the transport edge (the servers
# context's underscore spelling is mapped to ``DriverKind`` upstream; here we go
# from ``DriverKind`` to the proto enum).
_DRIVER_TO_PROTO: dict[DriverKind, "pb.ExecutionDriverKind.ValueType"] = {
    DriverKind.CONTAINER: pb.EXECUTION_DRIVER_KIND_CONTAINER,
}

# Map the domain launch mode to the wire enum (issue #307). FORGE_ARGSFILE drives
# the Worker's supervised-installer-then-args-file launch (issue #305/#306).
_LAUNCH_MODE_TO_PROTO: dict[LaunchMode, "pb.LaunchMode.ValueType"] = {
    LaunchMode.JAR: pb.LAUNCH_MODE_JAR,
    LaunchMode.FORGE_ARGSFILE: pb.LAUNCH_MODE_FORGE_ARGSFILE,
}

# Map the wire failure code onto the domain result code (CONTROL_PLANE.md 7).
_CODE_FROM_PROTO: dict[int, CommandResultCode] = {
    pb.COMMAND_ERROR_CODE_SERVER_NOT_FOUND: CommandResultCode.SERVER_NOT_FOUND,
    pb.COMMAND_ERROR_CODE_INVALID_STATE: CommandResultCode.INVALID_STATE,
    pb.COMMAND_ERROR_CODE_DRIVER_UNAVAILABLE: CommandResultCode.DRIVER_UNAVAILABLE,
    pb.COMMAND_ERROR_CODE_FILE_ACCESS_DENIED: CommandResultCode.FILE_ACCESS_DENIED,
    pb.COMMAND_ERROR_CODE_TRANSFER_FAILED: CommandResultCode.TRANSFER_FAILED,
    pb.COMMAND_ERROR_CODE_INTERNAL: CommandResultCode.INTERNAL,
    pb.COMMAND_ERROR_CODE_PORT_CONFLICT: CommandResultCode.PORT_CONFLICT,
    pb.COMMAND_ERROR_CODE_IMAGE_MISSING: CommandResultCode.IMAGE_MISSING,
    pb.COMMAND_ERROR_CODE_BUSY: CommandResultCode.BUSY,
}

# Map the wire file-access reason onto the domain reason (issue #548). An
# unrecognized value (an UNSPECIFIED, or a future reason an older API does not
# know) falls back to UNSPECIFIED, the generic path denial.
_FILE_ACCESS_REASON_FROM_PROTO: dict[int, FileAccessReason] = {
    pb.FILE_ACCESS_REASON_IS_A_DIRECTORY: FileAccessReason.IS_A_DIRECTORY,
    pb.FILE_ACCESS_REASON_NOT_A_DIRECTORY: FileAccessReason.NOT_A_DIRECTORY,
    pb.FILE_ACCESS_REASON_SYMLINK_REFUSED: FileAccessReason.SYMLINK_REFUSED,
    pb.FILE_ACCESS_REASON_PAYLOAD_TOO_LARGE: FileAccessReason.PAYLOAD_TOO_LARGE,
}


class ControlPlaneState:
    """Shared command-routing state between the servicer and the Port.

    One instance is created in the wiring layer and handed to both the servicer
    (to register sessions and resolve results) and :class:`GrpcControlPlane` (to
    dispatch). Mutations are synchronous, non-blocking dict operations on the one
    asyncio event loop, so no lock is needed under cooperative scheduling.

    ``late_snapshot_sink`` is the optional write-back for a final-snapshot result
    that arrives after its dispatch timed out or was cancelled and discarded the
    pending future (#891/#901): the held assignment is released immediately rather
    than waiting out the reconciler grace. Left ``None`` in tests that only
    exercise dispatch.
    """

    def __init__(
        self, *, late_snapshot_sink: LateSnapshotResultSink | None = None
    ) -> None:
        self._outbound: dict[WorkerId, asyncio.Queue[pb.ApiMessage]] = {}
        # worker -> the SessionToken that currently owns its outbound stream. A
        # reconnect replaces it, so a stale session's delayed teardown can be
        # told apart from the current one (mirrors close_session's queue-identity
        # guard; CONTROL_PLANE.md Section 4.4).
        self._sessions: dict[WorkerId, SessionToken] = {}
        # command_id -> (owning worker, result future). The worker is tracked so a
        # disconnect can fail exactly that worker's in-flight commands fast,
        # instead of letting them wait out the full command timeout.
        self._pending: dict[str, tuple[WorkerId, asyncio.Future[pb.CommandResult]]] = {}
        # command_id -> (dispatched worker, server id) for an IN-FLIGHT snapshot
        # command (issue #891). Held only while the future is pending so a timeout
        # or cancellation can promote the entry to ``_late_snapshots``; a normal
        # resolution drops it.
        self._snapshot_servers: dict[str, tuple[WorkerId, str]] = {}
        # command_id -> (dispatched worker, server id) for a SNAPSHOT whose pending
        # future was discarded on a dispatch timeout or cancellation (#891/#901). A
        # late CommandResult for it arrives unmatched (the future is gone); this
        # record lets :meth:`resolve` recognise it as a held final-snapshot result
        # and release the assignment via ``late_snapshot_sink`` instead of dropping
        # it. Bounded: only timed-out/cancelled snapshots land here, and each entry
        # is popped when its late result arrives (a fresh uuid4 command_id never
        # collides).
        self._late_snapshots: dict[str, tuple[WorkerId, str]] = {}
        self._late_snapshot_sink = late_snapshot_sink
        # Monotonic high-water mark of the latest session token opened per worker
        # (issue #1694). A stale session that resumes its awaits after a newer one
        # already called open_session is refused, preventing it from overwriting the
        # current outbound queue.
        self._latest_opened: dict[WorkerId, SessionToken] = {}

    def set_late_snapshot_sink(self, sink: LateSnapshotResultSink) -> None:
        """Bind the late-snapshot sink after construction (issue #891).

        The wiring builds this state first (the GrpcControlPlane adapter and the
        servers-backed sink both depend on it transitively), so the sink is injected
        once it exists rather than passed at construction. Tests that exercise the
        sink path can pass it via the constructor instead.
        """

        self._late_snapshot_sink = sink

    def open_session(
        self, worker_id: WorkerId, session: SessionToken
    ) -> asyncio.Queue[pb.ApiMessage]:
        """Register a fresh outbound queue for ``worker_id`` and return it.

        A reconnect replaces any prior queue, so the latest session owns the
        Worker's outbound stream. ``session`` is recorded as the current owner so
        :meth:`fail_worker_pending` can ignore a stale session's teardown.

        Any promoted late-snapshot records from a PRIOR session are swept here
        (issue #891): a new session for the same worker supersedes the old one, and
        ``fail_worker_pending`` is session-guarded, so a stale session's delayed
        teardown skips the sweep on a fast reconnect. The old session's upload died
        with its ctx (no late result will arrive to consume the record), so dropping
        it on the superseding session keeps the map from accreting.
        """

        if session < self._latest_opened.get(worker_id, session):
            raise StaleSessionError(worker_id.value, session)
        self._latest_opened[worker_id] = session
        for command_id in [
            cid for cid, (wid, _) in self._late_snapshots.items() if wid == worker_id
        ]:
            del self._late_snapshots[command_id]
        queue: asyncio.Queue[pb.ApiMessage] = asyncio.Queue()
        self._outbound[worker_id] = queue
        self._sessions[worker_id] = session
        return queue

    def close_session(
        self, worker_id: WorkerId, queue: asyncio.Queue[pb.ApiMessage]
    ) -> None:
        """Drop ``worker_id``'s outbound queue if it is still the current one.

        A stale session's teardown that no longer owns the current queue is
        ignored, mirroring the registry's reconnect-race handling
        (CONTROL_PLANE.md Section 4.4).
        """

        if self._outbound.get(worker_id) is queue:
            del self._outbound[worker_id]
            self._sessions.pop(worker_id, None)

    def register_pending(
        self,
        command_id: str,
        worker_id: WorkerId,
        *,
        snapshot_server_id: str | None = None,
    ) -> asyncio.Future[pb.CommandResult]:
        """Create and track a future awaiting the result for ``command_id``.

        ``worker_id`` is the worker the command was dispatched to, recorded so
        :meth:`fail_worker_pending` can fail it on that worker's disconnect.

        ``snapshot_server_id`` is set only for a snapshot command and names the
        target server; it is retained on a timeout or cancellation so a late
        result can be matched to the held assignment (#891/#901).
        """

        future: asyncio.Future[pb.CommandResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[command_id] = (worker_id, future)
        if snapshot_server_id is not None:
            self._snapshot_servers[command_id] = (worker_id, snapshot_server_id)
        return future

    def discard_pending(self, command_id: str) -> None:
        """Stop tracking ``command_id`` (on timeout or cancellation).

        For a SNAPSHOT command (issue #891), retain a (worker, server) record so a
        late ``CommandResult`` — the worker's transfer bound aborts a final-snapshot
        upload and reports ``TRANSFER_FAILED`` after the API abandoned the future
        (#874/#890), or a late SUCCESS — is recognised in :meth:`resolve` and used
        to release the held assignment immediately. Non-snapshot commands carry no
        such held state, so they are simply forgotten.
        """

        self._pending.pop(command_id, None)
        snapshot = self._snapshot_servers.pop(command_id, None)
        if snapshot is not None:
            self._late_snapshots[command_id] = snapshot

    async def resolve(
        self, command_id: str, worker_id: WorkerId, result: pb.CommandResult
    ) -> None:
        """Resolve the future waiting on ``command_id`` with ``result``.

        A result for an unknown/already-resolved command is ignored, so a late
        or duplicate ``CommandResult`` never crashes the inbound loop — except a
        late final-snapshot result, which :meth:`_clear_on_late_snapshot` uses to
        release the held assignment immediately (issue #891). Async only for that
        late-snapshot seam (the sink write is awaited like the status sink); the
        common match path stays synchronous, non-blocking dict work.

        ``worker_id`` is the worker that reported the result. A result from a
        worker that does not own the command is dropped with a warning and the
        command is left in flight to time out normally, mirroring the status
        sink's non-owning-worker guard (defense-in-depth, issue #789). A fresh
        uuid4 ``command_id`` is never shared across workers, so this only fires
        on a forged report; the owning worker's later result still resolves.
        """

        entry = self._pending.get(command_id)
        if entry is None:
            # No pending future: either an already-resolved/forged command, or a
            # late final-snapshot result for a dispatch that timed out or was
            # cancelled (#891/#901).
            await self._clear_on_late_snapshot(command_id, worker_id, result)
            return
        owning_worker, future = entry
        if owning_worker != worker_id:
            _LOG.warning(
                "dropping command result from non-owning worker",
                extra={
                    "command_id": command_id,
                    "reporting_worker_id": worker_id.value,
                    "owning_worker_id": owning_worker.value,
                },
            )
            return
        del self._pending[command_id]
        self._snapshot_servers.pop(command_id, None)
        if not future.done():
            future.set_result(result)

    async def _clear_on_late_snapshot(
        self, command_id: str, worker_id: WorkerId, result: pb.CommandResult
    ) -> None:
        """Release a held assignment on a late final-snapshot result (#891).

        Acts only when ``command_id`` is a snapshot whose dispatch timed out or
        was cancelled (its record survives in ``_late_snapshots``); any other
        unmatched result is dropped, as before. The #789 ownership guard: the
        reporting worker must be the worker the snapshot was dispatched to — a
        report from any other worker
        leaves the record in place and is ignored, so a forged late result cannot
        clear the assignment. The guarded clear at the servers seam re-checks
        ``assigned_worker_id == worker_id``, so a row a racing start re-placed is
        left untouched (defense-in-depth). On a SUCCESS the publish already landed,
        so the upload is done and there is no late publish for the #847 guard to
        fight; on a TRANSFER_FAILED the upload is dead — also no late publish.
        """

        snapshot = self._late_snapshots.get(command_id)
        if snapshot is None:
            return
        dispatched_worker, server_id = snapshot
        if dispatched_worker != worker_id:
            _LOG.warning(
                "dropping late snapshot result from non-owning worker",
                extra={
                    "command_id": command_id,
                    "reporting_worker_id": worker_id.value,
                    "dispatched_worker_id": dispatched_worker.value,
                },
            )
            return
        del self._late_snapshots[command_id]
        if self._late_snapshot_sink is None:
            return
        await self._late_snapshot_sink.clear_held_assignment_on_late_snapshot(
            server_id=server_id,
            worker_id=worker_id.value,
            succeeded=result.success,
        )

    def fail_worker_pending(
        self, worker_id: WorkerId, session: SessionToken, error: BaseException
    ) -> None:
        """Fail every in-flight command awaiting ``worker_id`` with ``error``.

        Invoked when the worker's session ends: its outbound stream is gone, so a
        pending command can never be answered. Failing fast (rather than waiting
        out the command timeout) unblocks awaiters with a typed
        :class:`WorkerNotConnectedError` immediately (CONTROL_PLANE.md 4.4).

        Guarded by ``session`` like :meth:`close_session`: a delayed teardown of a
        stale session that no longer owns ``worker_id``'s outbound stream is
        ignored, so it cannot spuriously fail a NEW session's in-flight futures
        after a reconnect (CONTROL_PLANE.md Section 4.4, reconnect race).
        """

        if self._sessions.get(worker_id) != session:
            return
        for command_id in [
            cid for cid, (wid, _) in self._pending.items() if wid == worker_id
        ]:
            _, future = self._pending.pop(command_id)
            # The worker's session is gone, so a snapshot it was running died with
            # its ctx (no late result will arrive, issue #847 DISCONNECT) — drop the
            # in-flight record so it is not promoted to a stale late-snapshot entry.
            self._snapshot_servers.pop(command_id, None)
            if not future.done():
                future.set_exception(error)
        # Drop any already-promoted late-snapshot records for this worker too: its
        # session ended, so no late result will arrive to consume them. Keyed by a
        # unique command_id, a leftover entry is harmless, but clearing it here keeps
        # the map from accreting one entry per disconnect-after-timeout (issue #891).
        for command_id in [
            cid for cid, (wid, _) in self._late_snapshots.items() if wid == worker_id
        ]:
            del self._late_snapshots[command_id]

    def outbound_for(self, worker_id: WorkerId) -> asyncio.Queue[pb.ApiMessage] | None:
        return self._outbound.get(worker_id)


def _to_api_command(command_id: str, server_id: str, command: Command) -> pb.ApiCommand:
    api = pb.ApiCommand(command_id=command_id, server_id=server_id)
    if isinstance(command, StartServerCommand):
        api.start.CopyFrom(
            pb.StartServer(
                driver=_DRIVER_TO_PROTO[command.driver],
                jar_relpath=command.jar_relpath,
                minecraft_version=command.minecraft_version,
                launch_mode=_LAUNCH_MODE_TO_PROTO[command.launch_mode],
                memory_limit_bytes=command.memory_limit_bytes,
                cpu_millis=command.cpu_millis,
            )
        )
    elif isinstance(command, StopServerCommand):
        api.stop.CopyFrom(pb.StopServer(force=command.force))
    elif isinstance(command, RestartServerCommand):
        api.restart.CopyFrom(pb.RestartServer())
    elif isinstance(command, ServerCommandCommand):
        api.server_command.CopyFrom(pb.ServerCommand(line=command.line))
    elif isinstance(command, HydrateCommand):
        api.hydrate.CopyFrom(
            pb.HydrateTrigger(
                transfer_url=command.transfer_url,
                transfer_token=command.transfer_token,
            )
        )
    elif isinstance(command, SnapshotCommand):
        api.snapshot.CopyFrom(
            pb.SnapshotTrigger(
                transfer_url=command.transfer_url,
                transfer_token=command.transfer_token,
            )
        )
    elif isinstance(command, ReadFileCommand):
        api.read_file.CopyFrom(pb.ReadFile(path=command.path))
    elif isinstance(command, EditFileCommand):
        api.edit_file.CopyFrom(pb.EditFile(path=command.path, content=command.content))
    elif isinstance(command, ListFilesCommand):
        api.list_files.CopyFrom(pb.ListFiles(path=command.path))
    elif isinstance(command, TunnelDialCommand):
        api.tunnel_dial.CopyFrom(
            pb.TunnelDial(
                server_id=server_id,
                endpoint=command.endpoint,
                token=command.token,
                tls_ca_pem=command.tls_ca_pem,
            )
        )
    elif isinstance(command, OpenBedrockTunnelCommand):
        api.open_bedrock_tunnel.CopyFrom(
            pb.OpenBedrockTunnel(
                server_id=server_id,
                relay_endpoint=command.relay_endpoint,
                bedrock_port=command.bedrock_port,
                token=command.token,
                tls_ca_pem=command.tls_ca_pem,
            )
        )
    elif isinstance(command, CloseBedrockTunnelCommand):
        api.close_bedrock_tunnel.CopyFrom(pb.CloseBedrockTunnel(server_id=server_id))
    else:  # pragma: no cover - exhaustive over the Command union
        raise TypeError(f"unsupported command type: {type(command)!r}")
    return api


def _to_result(message: pb.CommandResult) -> CommandResult:
    if message.success:
        # command_output / file_content / file_listing share the result oneof;
        # the scalar arms read back as the proto default (empty) when unset, so
        # passing both is safe. file_listing is a message, so map it only when it
        # is the arm actually set.
        listing = None
        if message.WhichOneof("result") == "file_listing":
            listing = _to_listing(message.file_listing)
        return CommandResult(
            code=CommandResultCode.OK,
            output=message.command_output,
            file_content=message.file_content,
            file_listing=listing,
        )
    code = _CODE_FROM_PROTO.get(message.error.code, CommandResultCode.INTERNAL)
    reason = _FILE_ACCESS_REASON_FROM_PROTO.get(
        message.error.file_access_reason, FileAccessReason.UNSPECIFIED
    )
    return CommandResult(
        code=code, message=message.error.message, file_access_reason=reason
    )


def _to_listing(message: pb.FileListing) -> FileListing:
    return FileListing(
        entries=tuple(
            FileEntry(name=e.name, is_dir=e.is_dir, size=e.size)
            for e in message.entries
        ),
        truncated=message.truncated,
    )


class GrpcControlPlane(ControlPlane):
    """:class:`ControlPlane` adapter over the shared :class:`ControlPlaneState`."""

    def __init__(self, state: ControlPlaneState, *, timeout_seconds: float) -> None:
        self._state = state
        self._timeout = timeout_seconds
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def dispatch(
        self,
        *,
        worker_id: WorkerId,
        server_id: str,
        command: Command,
        timeout_override: float | None = None,
        snapshot_is_final: bool = False,
    ) -> CommandResult:
        queue = self._state.outbound_for(worker_id)
        if queue is None:
            raise WorkerNotConnectedError(worker_id.value)
        command_id = str(uuid.uuid4())
        # A FINAL snapshot's server is recorded with the pending future so a timeout
        # or cancellation that discards the future still lets a late TRANSFER_FAILED
        # / SUCCESS result be matched to the held assignment (#891/#901). Only the
        # stop-flow final snapshot is tracked: it is the sole command whose stop
        # wedges the row at (stopped, stopped, assigned) for the held-snapshot
        # window. Periodic and on-demand snapshots share the command type but take
        # no worker reservation (running-id snapshots are reservation-free), so a
        # stale late result of theirs must NOT clear a subsequent final-snapshot
        # hold — they are untracked.
        snapshot_server_id = (
            server_id
            if snapshot_is_final and isinstance(command, SnapshotCommand)
            else None
        )
        future = self._state.register_pending(
            command_id, worker_id, snapshot_server_id=snapshot_server_id
        )
        api_command = _to_api_command(command_id, server_id, command)
        msg = pb.ApiMessage(correlation_id=command_id, api_command=api_command)
        msg.sent_at.CopyFrom(_now_timestamp())
        await queue.put(msg)
        # A longer per-command budget (the start's hydrate phase, issue #822)
        # overrides the default command deadline for this one dispatch.
        timeout = self._timeout if timeout_override is None else timeout_override
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            self._state.discard_pending(command_id)
            raise CommandTimedOutError(command_id) from exc
        except asyncio.CancelledError:
            # A cancelled dispatch (the awaiting HTTP-request task died at this
            # await, e.g. a client disconnect during the stop flow's final
            # snapshot) abandons the future exactly like a timeout: the worker
            # keeps working and may report late. Discard the pending entry so a
            # final snapshot's record is promoted and its late result clears the
            # held assignment (issue #901) instead of being dropped on the
            # matched path — and so a cancelled future never lingers in _pending.
            self._state.discard_pending(command_id)
            raise
        return _to_result(result)

    async def dispatch_fire_and_forget(
        self, *, worker_id: WorkerId, server_id: str, command: Command
    ) -> None:
        """Send ``command`` without awaiting its ``CommandResult`` (RELAY.md 4).

        The relay's TUNNEL decision dispatches a ``TunnelDial`` as a side effect:
        the Worker's real "result" is the dial-back arriving at the relay, so the
        ResolveJoin response must return immediately rather than blocking on the
        ``CommandResult``. The result is still correlated and logged for
        diagnostics by a detached task, which also discards the pending entry on
        timeout or cancellation (#1791) so the correlation map stays bounded.

        Raises :class:`WorkerNotConnectedError` synchronously when the Worker has
        no live session — the caller maps that to a STOPPED decision.
        """

        queue = self._state.outbound_for(worker_id)
        if queue is None:
            raise WorkerNotConnectedError(worker_id.value)
        command_id = str(uuid.uuid4())
        future = self._state.register_pending(command_id, worker_id)
        api_command = _to_api_command(command_id, server_id, command)
        msg = pb.ApiMessage(correlation_id=command_id, api_command=api_command)
        msg.sent_at.CopyFrom(_now_timestamp())
        await queue.put(msg)
        # Fire-and-forget: the result is not awaited here. The task is held in
        # _background_tasks so it is not GC-collected mid-flight; a done callback
        # removes it once the coroutine finishes.
        task = asyncio.create_task(
            self._log_fire_and_forget_result(command_id, server_id, future)
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _log_fire_and_forget_result(
        self,
        command_id: str,
        server_id: str,
        future: asyncio.Future[pb.CommandResult],
    ) -> None:
        try:
            result = await asyncio.wait_for(future, timeout=self._timeout)
        except (TimeoutError, WorkerNotConnectedError):
            # The dial-back never arriving is a relay-side timeout (RELAY.md
            # Section 10), recovered there; the API only notes the missing result.
            self._state.discard_pending(command_id)
            _LOG.info(
                "no CommandResult for fire-and-forget command",
                extra={"command_id": command_id, "server_id": server_id},
            )
            return
        except asyncio.CancelledError:
            # The logger task was cancelled at this await (process shutdown, or
            # GC of an abandoned task). Discard the entry so the correlation map
            # stays bounded, mirroring dispatch's cancellation arm (issue #1791).
            self._state.discard_pending(command_id)
            raise
        if result.success:
            _LOG.debug(
                "fire-and-forget command acknowledged",
                extra={"command_id": command_id, "server_id": server_id},
            )
        else:
            _LOG.warning(
                "fire-and-forget command failed",
                extra={
                    "command_id": command_id,
                    "server_id": server_id,
                    "error": result.error.message,
                },
            )
