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
3. **Steady state.** ``Event{Heartbeat}`` refreshes liveness (FR-WRK-2); other
   events (status / log / metrics) are accepted and ignored — their consumers
   are later epics (#10). Command sending is out of scope (#82+).
4. **Disconnect.** When the stream ends (clean close or transport error) the
   Worker is marked offline (FR-WRK-4).

Only this module (and the wiring layer) touches grpcio; the domain and
application layers stay transport-free (ARCHITECTURE.md Section 2.1). The
generated stubs are excluded from strict typing (pyproject ``[tool.mypy]``), so
the few interactions with them are annotated pragmatically.
"""

from __future__ import annotations

import datetime as dt
import hmac
import logging
from collections.abc import AsyncIterator

import grpc
from grpc import aio

from mc_server_dashboard_api.fleet.domain.clock import Clock
from mc_server_dashboard_api.fleet.domain.entities import Worker
from mc_server_dashboard_api.fleet.domain.errors import InvalidWorkerIdError
from mc_server_dashboard_api.fleet.domain.registry import SessionToken, WorkerRegistry
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

# Metadata key carrying the shared Worker credential (NFR-SEC-1). gRPC lowercases
# metadata keys; the value is "Bearer <credential>" by convention.
_AUTH_METADATA_KEY = "authorization"
_BEARER_PREFIX = "Bearer "

# The API advertises a heartbeat interval a few times tighter than its liveness
# timeout so a Worker normally beats several times before the window lapses
# (CONTROL_PLANE.md Section 4.3).
_HEARTBEAT_INTERVAL_DIVISOR = 3

_DRIVER_BY_KIND: dict[int, DriverKind] = {
    pb.EXECUTION_DRIVER_KIND_HOST_PROCESS: DriverKind.HOST_PROCESS,
    pb.EXECUTION_DRIVER_KIND_CONTAINER: DriverKind.CONTAINER,
}


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
    ) -> None:
        self._registry = registry
        self._clock = clock
        self._credential = worker_credential
        self._heartbeat_interval = heartbeat_timeout / _HEARTBEAT_INTERVAL_DIVISOR

    async def Session(  # noqa: N802 (gRPC-generated method name)
        self,
        request_iterator: AsyncIterator[pb.WorkerMessage],
        context: aio.ServicerContext,
    ) -> AsyncIterator[pb.ApiMessage]:
        await self._authenticate(context)

        worker_id, correlation_id, session = await self._register(
            request_iterator, context
        )
        try:
            yield self._register_ack(correlation_id=correlation_id)
            async for message in request_iterator:
                self._handle(worker_id, message)
        finally:
            # Pass this Session's token so a delayed teardown only offlines the
            # Worker if it has not reconnected on a newer Session (Section 4.4).
            self._registry.mark_disconnected(worker_id, session)
            _LOG.info("worker disconnected", extra={"worker_id": worker_id.value})

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
        try:
            worker_id = WorkerId(register.worker_id)
        except InvalidWorkerIdError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid worker id")

        now = self._clock.now()
        session = self._registry.register(
            Worker(
                id=worker_id,
                version=register.worker_version,
                capabilities=_capabilities_from_proto(register.capabilities),
                registered_at=now,
                last_heartbeat_at=now,
            )
        )
        _LOG.info("worker registered", extra={"worker_id": worker_id.value})
        return worker_id, first.correlation_id, session

    def _handle(self, worker_id: WorkerId, message: pb.WorkerMessage) -> None:
        if message.WhichOneof("payload") != "event":
            # Command results have no API-side consumer yet (#82+); accept and
            # ignore so a well-behaved Worker is never disconnected for them.
            return
        if message.event.WhichOneof("event") == "heartbeat":
            self._registry.record_heartbeat(worker_id, self._clock.now())
        # StatusChange / LogLine / Metrics are accepted and ignored; their
        # consumers are later epics (#10).

    def _register_ack(self, *, correlation_id: str) -> pb.ApiMessage:
        ack = pb.RegisterAck(accepted=True)
        ack.heartbeat_interval.FromTimedelta(self._heartbeat_interval)
        return pb.ApiMessage(correlation_id=correlation_id, register_ack=ack)


def make_grpc_server(
    *,
    registry: WorkerRegistry,
    clock: Clock,
    worker_credential: str,
    heartbeat_timeout: dt.timedelta,
    host: str,
    port: int,
) -> aio.Server:
    """Build (but do not start) the control-plane gRPC server bound to host:port."""

    server = aio.server()
    servicer = WorkerSessionServicer(
        registry=registry,
        clock=clock,
        worker_credential=worker_credential,
        heartbeat_timeout=heartbeat_timeout,
    )
    add_WorkerServiceServicer_to_server(servicer, server)
    server.add_insecure_port(f"{host}:{port}")
    return server
