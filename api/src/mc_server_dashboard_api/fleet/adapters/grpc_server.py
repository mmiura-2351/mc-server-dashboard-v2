"""The control-plane gRPC server: hosts ``WorkerService.Session`` (Section 5.1).

This is the edge adapter for the API↔Worker control plane. It implements the
single bidirectional RPC and enforces the stream lifecycle from
CONTROL_PLANE.md Section 4:

1. **Authenticate** the stream from a shared credential carried in call
   metadata (``authorization: Bearer <credential>``, NFR-SEC-1). A missing or
   wrong credential aborts the stream with ``UNAUTHENTICATED`` before any
   message is processed.
2. **Register first.** The first ``WorkerMessage`` MUST carry ``Register``
   (FR-WRK-1); anything else aborts with ``FAILED_PRECONDITION``. On success the
   Worker is added to the :class:`WorkerRegistry` and the API replies
   ``RegisterAck{accepted, heartbeat_interval}``.
3. **Steady state.** ``Event{Heartbeat}`` refreshes liveness (FR-WRK-2);
   ``Event{StatusChange}`` reconciles the server's observed state through the
   :class:`ServerStateSink` (FR-SRV-4); ``CommandResult`` resolves the pending
   correlation so a dispatched command's awaiter unblocks (CONTROL_PLANE.md
   Sections 3, 5). ``StatusChange`` / ``LogLine`` / ``Metrics`` are relayed to
   subscribed clients through the :class:`RealTimeEvents` Port (FR-MON-1..3); the
   publish is non-blocking, so a slow subscriber never back-pressures the stream.
   The API may also push ``ApiCommand`` messages on this stream: they ride the
   Worker's outbound queue, drained by the ``Session`` generator.
4. **Disconnect.** When the stream ends (clean close or transport error) the
   Worker is marked offline and its servers' observed state set to ``unknown``
   (FR-WRK-4).

Only this module (and the wiring layer) touches grpcio; the domain and
application layers stay transport-free (ARCHITECTURE.md Section 2.1). The
generated stubs are excluded from strict typing (pyproject ``[tool.mypy]``), so
the few interactions with them are annotated pragmatically.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import logging
import uuid
from collections.abc import AsyncIterator

import grpc
from grpc import aio

from mc_server_dashboard_api.fleet.adapters.control_plane import ControlPlaneState
from mc_server_dashboard_api.fleet.domain.clock import Clock
from mc_server_dashboard_api.fleet.domain.control_plane import WorkerNotConnectedError
from mc_server_dashboard_api.fleet.domain.entities import Worker
from mc_server_dashboard_api.fleet.domain.errors import InvalidWorkerIdError
from mc_server_dashboard_api.fleet.domain.real_time_events import (
    EventStream,
    RealTimeEvent,
    RealTimeEvents,
)
from mc_server_dashboard_api.fleet.domain.registry import SessionToken, WorkerRegistry
from mc_server_dashboard_api.fleet.domain.server_state_sink import ServerStateSink
from mc_server_dashboard_api.fleet.domain.value_objects import (
    DriverKind,
    HostResources,
    WorkerCapabilities,
    WorkerId,
)
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from mcsd.controlplane.v1.control_plane_pb2_grpc import (
    WorkerServiceServicer,
    add_WorkerServiceServicer_to_server,
)

_LOG = logging.getLogger(__name__)

# Map the wire observed-state enum onto the sink's state string (CONTROL_PLANE.md
# Section 6). An unspecified/unknown value has no mapping and is dropped — a
# well-behaved Worker only reports the documented states.
_STATE_BY_PROTO: dict[int, str] = {
    pb.SERVER_STATE_STARTING: "starting",
    pb.SERVER_STATE_RUNNING: "running",
    pb.SERVER_STATE_STOPPING: "stopping",
    pb.SERVER_STATE_STOPPED: "stopped",
    pb.SERVER_STATE_RESTARTING: "restarting",
    pb.SERVER_STATE_CRASHED: "crashed",
}

# Metadata key carrying the shared Worker credential (NFR-SEC-1). gRPC lowercases
# metadata keys; the value is "Bearer <credential>" by convention.
_AUTH_METADATA_KEY = "authorization"
_BEARER_PREFIX = "Bearer "

# The API advertises a heartbeat interval a few times tighter than its liveness
# timeout so a Worker normally beats several times before the window lapses
# (CONTROL_PLANE.md Section 4.3).
_HEARTBEAT_INTERVAL_DIVISOR = 3

# Cap on consecutive per-message handler failures before the session is treated
# as terminal (issue #807). The per-message log-and-continue (issue #776)
# contains a single bad event, but an unbounded skip keeps the session alive
# against a permanently poisoned sink (e.g. the DB is down): every StatusChange
# is discarded while the worker stays "online" and its servers are never marked
# unknown, silently hiding the outage from the reconciler. After this many
# consecutive failures, letting the next one propagate ends the session, which
# marks the worker disconnected and surfaces the outage. The counter resets on
# any successful handle, so transient blips never accumulate to the cap. The
# value is small enough to fail fast yet absorbs a short burst of transient
# errors without bouncing a healthy session.
_MAX_CONSECUTIVE_HANDLER_FAILURES = 5

_DRIVER_BY_KIND: dict[int, DriverKind] = {
    pb.EXECUTION_DRIVER_KIND_HOST_PROCESS: DriverKind.HOST_PROCESS,
    pb.EXECUTION_DRIVER_KIND_CONTAINER: DriverKind.CONTAINER,
}

# Map the wire log-stream enum onto the relayed payload string (FR-MON-2). An
# unspecified value is relayed as "stdout" — the conservative default.
_LOG_STREAM_BY_PROTO: dict[int, str] = {
    pb.LOG_STREAM_STDOUT: "stdout",
    pb.LOG_STREAM_STDERR: "stderr",
}


def _emitted_at_from_proto(message: pb.WorkerMessage) -> dt.datetime | None:
    """Return the Worker's authoritative event time, or None when unset/zero.

    A WorkerMessage omits ``emitted_at`` (or leaves it at the zero Timestamp)
    when the Worker does not stamp it; the relay then falls back to receive
    time downstream, so this returns None for both cases. The returned datetime
    is timezone-aware (UTC), matching the proto Timestamp contract.
    """

    if not message.HasField("emitted_at"):
        return None
    emitted = message.emitted_at.ToDatetime(tzinfo=dt.timezone.utc)
    if emitted == dt.datetime.fromtimestamp(0, tz=dt.timezone.utc):
        return None
    return emitted


def _capabilities_from_proto(caps: pb.WorkerCapabilities) -> WorkerCapabilities:
    drivers = {
        _DRIVER_BY_KIND[kind] for kind in caps.drivers if kind in _DRIVER_BY_KIND
    }
    resources = HostResources(
        cpu_cores=caps.resources.cpu_cores,
        memory_bytes=caps.resources.memory_bytes,
    )
    return WorkerCapabilities(
        drivers=frozenset(drivers),
        max_servers=caps.max_servers,
        resources=resources,
    )


class WorkerSessionServicer(WorkerServiceServicer):
    """Servicer enforcing the control-plane stream lifecycle (Section 4)."""

    def __init__(
        self,
        *,
        registry: WorkerRegistry,
        clock: Clock,
        worker_credential: str,
        heartbeat_timeout: dt.timedelta,
        control_plane: ControlPlaneState,
        state_sink: ServerStateSink,
        real_time_events: RealTimeEvents,
    ) -> None:
        self._registry = registry
        self._clock = clock
        self._credential = worker_credential
        self._heartbeat_interval = heartbeat_timeout / _HEARTBEAT_INTERVAL_DIVISOR
        self._control_plane = control_plane
        self._state_sink = state_sink
        self._real_time_events = real_time_events

    async def Session(  # noqa: N802 (gRPC-generated method name)
        self,
        request_iterator: AsyncIterator[pb.WorkerMessage],
        context: aio.ServicerContext,
    ) -> AsyncIterator[pb.ApiMessage]:
        await self._authenticate(context)

        worker_id, correlation_id, session = await self._register(
            request_iterator, context
        )
        # The registry resets this Worker's load to zero on (re)registration;
        # rebuild it from the authoritative running-server tally so placement is
        # correct after a reconnect (epic #7 reconciliation obligation).
        await self._rebuild_assignments(worker_id)
        outbound = self._control_plane.open_session(worker_id, session)
        # Read inbound events/results in a background task while this generator
        # yields the Worker's outbound commands; both share the one stream.
        reader = asyncio.ensure_future(self._read_inbound(worker_id, request_iterator))
        # Tracks the in-flight outbound.get() so the finally can cancel it even if
        # this generator is cancelled mid-await (an abrupt gRPC RPC cancel): an
        # uncancelled get() would await the orphaned queue forever, leaking a
        # Task+Queue per disconnect (issue #788).
        outbound_get: asyncio.Future[pb.ApiMessage] | None = None
        try:
            yield self._register_ack(correlation_id=correlation_id)
            while True:
                outbound_get = asyncio.ensure_future(outbound.get())
                done, _ = await asyncio.wait(
                    {outbound_get, reader}, return_when=asyncio.FIRST_COMPLETED
                )
                if outbound_get in done:
                    yield outbound_get.result()
                else:
                    # The inbound stream ended; stop yielding and tear down.
                    outbound_get.cancel()
                    break
        finally:
            if outbound_get is not None:
                outbound_get.cancel()
            reader.cancel()
            # Retrieve the reader's outcome so a transport error it raised is not
            # logged as an unretrieved task exception; a cancellation is expected.
            # A genuine terminal error (e.g. a transport drop, or an unexpected
            # error escaping the handler) is logged at warning with the worker id
            # so the cause is not silently lost (issue #776).
            try:
                await reader
            except asyncio.CancelledError:
                pass
            except Exception:
                _LOG.warning(
                    "worker session reader ended with an error",
                    extra={"worker_id": worker_id.value},
                    exc_info=True,
                )
            # Fail this worker's in-flight commands immediately: its outbound
            # stream is gone, so they can never be answered. Awaiters get a typed
            # WorkerNotConnectedError now instead of riding the full timeout.
            # Guarded by this Session's token so a stale teardown after a
            # reconnect does not fail the NEW session's in-flight futures
            # (CONTROL_PLANE.md Section 4.4). Runs before close_session, which
            # drops this worker's session record.
            self._control_plane.fail_worker_pending(
                worker_id, session, WorkerNotConnectedError(worker_id.value)
            )
            self._control_plane.close_session(worker_id, outbound)
            # Pass this Session's token so a delayed teardown only offlines the
            # Worker if it has not reconnected on a newer Session (Section 4.4).
            self._registry.mark_disconnected(worker_id, session)
            # Guard the bulk observed=unknown write with this Session's token too:
            # the write deliberately bypasses the per-server monotonic guard, so a
            # stale teardown after a reconnect would otherwise clobber the NEW
            # session's live server states (issue #775, CONTROL_PLANE.md 4.4).
            # There is an accepted one-roundtrip TOCTOU window here: a reconnect
            # on a newer Session could win the is_current_session check between
            # this read and the bulk unknown-write below, so the write could
            # still clobber the NEW session's live states with unknown. The
            # window is judged acceptable (PR #802 review): it is bounded to a
            # single observation and the reconciler self-heals — the live worker
            # re-reports its servers' states on its next StatusChange, restoring
            # them. Closing it fully would need the write itself to be guarded by
            # the session token atomically, which is not worth the complexity.
            if self._registry.is_current_session(worker_id, session):
                await self._state_sink.mark_worker_servers_unknown(
                    worker_id=worker_id.value
                )
            _LOG.info("worker disconnected", extra={"worker_id": worker_id.value})

    async def _read_inbound(
        self,
        worker_id: WorkerId,
        request_iterator: AsyncIterator[pb.WorkerMessage],
    ) -> None:
        consecutive_failures = 0
        async for message in request_iterator:
            # Contain a per-message handler failure (e.g. a transient DB error
            # while recording one StatusChange) so a single bad event is logged
            # and skipped rather than tearing down the whole worker session
            # (issue #776). Stream-level errors from iterating request_iterator
            # are not caught here, so a real transport drop still ends the loop;
            # CancelledError propagates so teardown is not swallowed.
            #
            # The skip is bounded (issue #807): a permanently poisoned sink would
            # otherwise keep discarding every event while the session stays alive
            # and the worker's servers are never marked unknown. Once the failures
            # have already reached the cap, the next failure is allowed to
            # propagate, ending the session so teardown marks the worker
            # disconnected and surfaces the outage to the reconciler. Any success
            # resets the streak, so only a sustained outage trips the cap.
            try:
                await self._handle(worker_id, message)
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_failures += 1
                if consecutive_failures > _MAX_CONSECUTIVE_HANDLER_FAILURES:
                    _LOG.error(
                        "ending worker session after %d consecutive handler "
                        "failures; the state sink looks unavailable",
                        consecutive_failures,
                        extra={"worker_id": worker_id.value},
                        exc_info=True,
                    )
                    raise
                _LOG.warning(
                    "dropping a worker message after a handler error",
                    extra={"worker_id": worker_id.value},
                    exc_info=True,
                )
            else:
                consecutive_failures = 0

    async def _rebuild_assignments(self, worker_id: WorkerId) -> None:
        # Snapshot the confirm epoch BEFORE reading the DB tally (#844): a placement
        # that commits AND confirms in the await below stamps a later epoch, so
        # set_assignment keeps that +1 rather than overwriting it with this stale
        # tally (which cannot yet see the just-landed commit).
        snapshot_epoch = self._registry.assignment_epoch(worker_id)
        assignments = await self._state_sink.running_assignment_ids(
            worker_id=worker_id.value
        )
        self._registry.set_assignment(worker_id, assignments, snapshot_epoch)

    async def _authenticate(
        self,
        context: aio.ServicerContext,
    ) -> None:
        presented = self._credential_from_metadata(context)
        if presented is None or not hmac.compare_digest(presented, self._credential):
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED, "worker credential rejected"
            )

    @staticmethod
    def _credential_from_metadata(
        context: aio.ServicerContext,
    ) -> str | None:
        for key, value in context.invocation_metadata() or ():
            text = str(value)
            if key == _AUTH_METADATA_KEY and text.startswith(_BEARER_PREFIX):
                return text[len(_BEARER_PREFIX) :]
        return None

    async def _register(
        self,
        request_iterator: AsyncIterator[pb.WorkerMessage],
        context: aio.ServicerContext,
    ) -> tuple[WorkerId, str, SessionToken]:
        try:
            first = await request_iterator.__anext__()
        except StopAsyncIteration:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "stream closed before Register",
            )

        if first.WhichOneof("payload") != "register":
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "first message must be Register",
            )

        register = first.register
        # The API persists a server's assigned worker as a UUID column
        # (assigned_worker_id), and the servers/fleet seam bridges str <-> UUID
        # at registration. Enforce the format here so a non-UUID worker id is
        # rejected loudly instead of silently breaking observed-state and
        # assignment tracking downstream (issue #99).
        try:
            worker_id = WorkerId(register.worker_id)
            uuid.UUID(register.worker_id)
        except (InvalidWorkerIdError, ValueError):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "worker id must be a UUID (CONFIGURATION.md worker.id)",
            )

        now = self._clock.now()
        session = self._registry.register(
            Worker(
                id=worker_id,
                version=register.worker_version,
                capabilities=_capabilities_from_proto(register.capabilities),
                registered_at=now,
                last_heartbeat_at=now,
            ),
            # The working sets the Worker reports already on its persistent scratch,
            # mapped to the generation each is at (issue #763); recorded so the
            # lifecycle layer skips the destructive hydrate on a same-worker restart
            # only when the held generation is fresh enough. A duplicate server id in
            # the repeated field keeps the last (a malformed Worker would be a bug).
            held_servers={hs.server_id: hs.generation for hs in register.held_servers},
        )
        _LOG.info("worker registered", extra={"worker_id": worker_id.value})
        return worker_id, first.correlation_id, session

    async def _handle(self, worker_id: WorkerId, message: pb.WorkerMessage) -> None:
        payload = message.WhichOneof("payload")
        if payload == "command_result":
            # Match the result to its in-flight command by command_id, carried as
            # the enclosing message's correlation_id (CONTROL_PLANE.md Section 3).
            # The reporting worker is passed so a result forged for another
            # worker's command is dropped, not applied (issue #789).
            self._control_plane.resolve(
                message.correlation_id, worker_id, message.command_result
            )
            return
        if payload != "event":
            return
        # The Worker's authoritative event time, carried once on the enclosing
        # message; relayed so a queued subscriber sees true event time rather
        # than the relay's send time. None when the Worker left it unset/zero.
        emitted_at = _emitted_at_from_proto(message)
        event = message.event.WhichOneof("event")
        if event == "heartbeat":
            self._registry.record_heartbeat(worker_id, self._clock.now())
        elif event == "status_change":
            await self._reconcile_status(worker_id, message.event, emitted_at)
        elif event == "log_line":
            self._relay_log(message.event, emitted_at)
        elif event == "metrics":
            self._relay_metrics(message.event, emitted_at)

    async def _reconcile_status(
        self, worker_id: WorkerId, event: pb.Event, emitted_at: dt.datetime | None
    ) -> None:
        state = _STATE_BY_PROTO.get(event.status_change.state)
        if state is None or not event.server_id:
            return
        await self._state_sink.record_observed_state(
            server_id=event.server_id, worker_id=worker_id.value, state=state
        )
        # Relay the observed transition to subscribed clients (FR-MON-1). The
        # publish is synchronous and best-effort: it never awaits subscriber
        # consumption, so a slow client cannot back-pressure this session path.
        self._real_time_events.publish(
            server_id=event.server_id,
            event=RealTimeEvent(
                stream=EventStream.STATUS,
                payload={"state": state, "detail": event.status_change.detail},
                emitted_at=emitted_at,
            ),
        )

    def _relay_log(self, event: pb.Event, emitted_at: dt.datetime | None) -> None:
        """Relay a server log line to subscribed clients (FR-MON-2)."""

        if not event.server_id:
            return
        self._real_time_events.publish(
            server_id=event.server_id,
            event=RealTimeEvent(
                stream=EventStream.LOG,
                payload={
                    "line": event.log_line.line,
                    "stream": _LOG_STREAM_BY_PROTO.get(event.log_line.stream, "stdout"),
                },
                emitted_at=emitted_at,
            ),
        )

    def _relay_metrics(self, event: pb.Event, emitted_at: dt.datetime | None) -> None:
        """Relay a runtime-metrics sample to subscribed clients (FR-MON-3)."""

        if not event.server_id:
            return
        self._real_time_events.publish(
            server_id=event.server_id,
            event=RealTimeEvent(
                stream=EventStream.METRICS,
                payload={
                    "cpu_millis": event.metrics.cpu_millis,
                    "memory_bytes": event.metrics.memory_bytes,
                    "player_count": event.metrics.player_count,
                },
                emitted_at=emitted_at,
            ),
        )

    def _register_ack(self, *, correlation_id: str) -> pb.ApiMessage:
        ack = pb.RegisterAck(accepted=True)
        ack.heartbeat_interval.FromTimedelta(self._heartbeat_interval)
        return pb.ApiMessage(correlation_id=correlation_id, register_ack=ack)


def _bind_port(
    server: aio.Server,
    *,
    address: str,
    cert_file: str | None,
    key_file: str | None,
    insecure: bool,
) -> None:
    """Bind the listener over TLS when a cert/key pair is given, else plaintext.

    Server-side TLS encrypts the control channel (NFR-SEC-1): with both
    ``cert_file`` and ``key_file`` the listener uses
    ``grpc.ssl_server_credentials`` (no client-cert verification — M1 ships
    server-side TLS only; the Worker credential authenticates the Worker). With
    neither, ``insecure`` must be true and the listener binds plaintext with a
    loud startup WARNING for local/dev. The required-unless-insecure rule itself
    is enforced upstream at the edge (app factory), mirroring the Worker.
    """

    if cert_file is not None and key_file is not None:
        with open(key_file, "rb") as key_handle:
            private_key = key_handle.read()
        with open(cert_file, "rb") as cert_handle:
            certificate_chain = cert_handle.read()
        credentials = grpc.ssl_server_credentials([(private_key, certificate_chain)])
        server.add_secure_port(address, credentials)
        return
    _LOG.warning(
        "control-plane gRPC server bound WITHOUT TLS (control.tls.insecure=true); "
        "use only for local development"
    )
    server.add_insecure_port(address)


def make_grpc_server(
    *,
    registry: WorkerRegistry,
    clock: Clock,
    worker_credential: str,
    heartbeat_timeout: dt.timedelta,
    control_plane: ControlPlaneState,
    state_sink: ServerStateSink,
    real_time_events: RealTimeEvents,
    host: str,
    port: int,
    cert_file: str | None = None,
    key_file: str | None = None,
    insecure: bool = False,
) -> aio.Server:
    """Build (but do not start) the control-plane gRPC server bound to host:port.

    The listener serves over TLS when ``cert_file``/``key_file`` are given, else
    plaintext when ``insecure`` is set (NFR-SEC-1). The required-unless-insecure
    rule is enforced at the edge before this is called.
    """

    server = aio.server()
    servicer = WorkerSessionServicer(
        registry=registry,
        clock=clock,
        worker_credential=worker_credential,
        heartbeat_timeout=heartbeat_timeout,
        control_plane=control_plane,
        state_sink=state_sink,
        real_time_events=real_time_events,
    )
    add_WorkerServiceServicer_to_server(servicer, server)
    _bind_port(
        server,
        address=f"{host}:{port}",
        cert_file=cert_file,
        key_file=key_file,
        insecure=insecure,
    )
    return server
