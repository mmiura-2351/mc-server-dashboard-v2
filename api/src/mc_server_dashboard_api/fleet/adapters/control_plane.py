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

from mc_server_dashboard_api.fleet.domain.control_plane import (
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
    ReadFileCommand,
    RestartServerCommand,
    ServerCommandCommand,
    SnapshotCommand,
    StartServerCommand,
    StopServerCommand,
    WorkerNotConnectedError,
)
from mc_server_dashboard_api.fleet.domain.registry import SessionToken
from mc_server_dashboard_api.fleet.domain.value_objects import DriverKind, WorkerId
from mcsd.controlplane.v1 import control_plane_pb2 as pb

_LOG = logging.getLogger(__name__)

# Map the domain driver kind to the wire enum at the transport edge (the servers
# context's underscore spelling is mapped to ``DriverKind`` upstream; here we go
# from ``DriverKind`` to the proto enum).
_DRIVER_TO_PROTO: dict[DriverKind, "pb.ExecutionDriverKind.ValueType"] = {
    DriverKind.HOST_PROCESS: pb.EXECUTION_DRIVER_KIND_HOST_PROCESS,
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
    """

    def __init__(self) -> None:
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

    def open_session(
        self, worker_id: WorkerId, session: SessionToken
    ) -> asyncio.Queue[pb.ApiMessage]:
        """Register a fresh outbound queue for ``worker_id`` and return it.

        A reconnect replaces any prior queue, so the latest session owns the
        Worker's outbound stream. ``session`` is recorded as the current owner so
        :meth:`fail_worker_pending` can ignore a stale session's teardown.
        """

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
        self, command_id: str, worker_id: WorkerId
    ) -> asyncio.Future[pb.CommandResult]:
        """Create and track a future awaiting the result for ``command_id``.

        ``worker_id`` is the worker the command was dispatched to, recorded so
        :meth:`fail_worker_pending` can fail it on that worker's disconnect.
        """

        future: asyncio.Future[pb.CommandResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[command_id] = (worker_id, future)
        return future

    def discard_pending(self, command_id: str) -> None:
        """Stop tracking ``command_id`` (on timeout or after resolution)."""

        self._pending.pop(command_id, None)

    def resolve(
        self, command_id: str, worker_id: WorkerId, result: pb.CommandResult
    ) -> None:
        """Resolve the future waiting on ``command_id`` with ``result``.

        A result for an unknown/already-resolved command is ignored, so a late
        or duplicate ``CommandResult`` never crashes the inbound loop.

        ``worker_id`` is the worker that reported the result. A result from a
        worker that does not own the command is dropped with a warning and the
        command is left in flight to time out normally, mirroring the status
        sink's non-owning-worker guard (defense-in-depth, issue #789). A fresh
        uuid4 ``command_id`` is never shared across workers, so this only fires
        on a forged report; the owning worker's later result still resolves.
        """

        entry = self._pending.get(command_id)
        if entry is None:
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
        if not future.done():
            future.set_result(result)

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
            if not future.done():
                future.set_exception(error)

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

    async def dispatch(
        self, *, worker_id: WorkerId, server_id: str, command: Command
    ) -> CommandResult:
        queue = self._state.outbound_for(worker_id)
        if queue is None:
            raise WorkerNotConnectedError(worker_id.value)
        command_id = str(uuid.uuid4())
        future = self._state.register_pending(command_id, worker_id)
        api_command = _to_api_command(command_id, server_id, command)
        await queue.put(
            pb.ApiMessage(correlation_id=command_id, api_command=api_command)
        )
        try:
            result = await asyncio.wait_for(future, timeout=self._timeout)
        except TimeoutError as exc:
            self._state.discard_pending(command_id)
            raise CommandTimedOutError(command_id) from exc
        return _to_result(result)
