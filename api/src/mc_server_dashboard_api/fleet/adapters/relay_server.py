"""The ``RelayService`` gRPC servicer (RELAY.md Sections 4, 6; issue #956).

Served on the API's existing gRPC listener (``server.grpc_port``) alongside
``WorkerService``, only when ``relay.enabled``. Transport posture mirrors the
Worker (CONTROL_PLANE.md Section 2): server-side TLS plus a shared-secret
credential in call metadata — but a *separate* credential (``relay.credential``)
so relay and Worker credentials rotate independently. All three RPCs are unary
and relay-initiated; the relay has no command inbox.

- ``Register`` stores the relay's tunnel endpoint + CA PEM (last-writer-wins, one
  relay per deployment), closes any open ``game_session`` rows absent from the
  registration's active set (orphan healing, RELAY.md Sections 6, 10), and returns
  ``base_domain``.
- ``ResolveJoin`` maps a slug to a routing decision (NOT_FOUND / STOPPED /
  TUNNEL). On TUNNEL it mints a single-use token, dispatches a ``TunnelDial`` to
  the assigned Worker fire-and-forget, and returns ``{TUNNEL, token, server_id}``.
- ``ReportSessions`` persists batched session lifecycle events through the
  ``SessionSink`` Port: idempotent start (insert-or-ignore) and end (set
  ``ended_at``) upserts keyed on the relay-minted ``session_id`` (issue #957).

Only this module (and the wiring) touches grpcio / the generated stubs; the
domain stays transport-free (ARCHITECTURE.md Section 2.1).
"""

from __future__ import annotations

import datetime as dt
import hmac
import logging

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from grpc import aio

from mc_server_dashboard_api.fleet.adapters.control_plane import GrpcControlPlane
from mc_server_dashboard_api.fleet.adapters.relay_state import (
    JoinTokenTable,
    RelayRegistration,
)
from mc_server_dashboard_api.fleet.domain.clock import Clock
from mc_server_dashboard_api.fleet.domain.control_plane import (
    TunnelDialCommand,
    WorkerNotConnectedError,
)
from mc_server_dashboard_api.fleet.domain.entities import WorkerStatus
from mc_server_dashboard_api.fleet.domain.errors import InvalidWorkerIdError
from mc_server_dashboard_api.fleet.domain.registry import WorkerRegistry
from mc_server_dashboard_api.fleet.domain.server_route_resolver import (
    ServerRoute,
    ServerRouteResolver,
)
from mc_server_dashboard_api.fleet.domain.session_sink import (
    SessionSink,
    SessionStart,
)
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from mcsd.relay.v1 import relay_pb2 as pb
from mcsd.relay.v1.relay_pb2_grpc import (
    RelayServiceServicer,
    add_RelayServiceServicer_to_server,
)

_LOG = logging.getLogger(__name__)

# Metadata key carrying the shared relay credential (NFR-SEC-1). gRPC lowercases
# metadata keys; the value is "Bearer <credential>" by convention, mirroring the
# Worker channel (grpc_server.py).
_AUTH_METADATA_KEY = "authorization"
_BEARER_PREFIX = "Bearer "


class RelayServicer(RelayServiceServicer):
    """Servicer for the relay-to-API contract (RELAY.md Section 6)."""

    def __init__(
        self,
        *,
        credential: str,
        base_domain: str,
        registration: RelayRegistration,
        token_table: JoinTokenTable,
        resolver: ServerRouteResolver,
        registry: WorkerRegistry,
        control_plane: GrpcControlPlane,
        session_sink: SessionSink,
        clock: Clock,
    ) -> None:
        self._credential = credential
        self._base_domain = base_domain
        self._registration = registration
        self._token_table = token_table
        self._resolver = resolver
        self._registry = registry
        self._control_plane = control_plane
        self._session_sink = session_sink
        self._clock = clock

    async def Register(  # noqa: N802 (gRPC-generated method name)
        self, request: pb.RegisterRequest, context: aio.ServicerContext
    ) -> pb.RegisterResponse:
        await self._authenticate(context)
        self._registration.set(
            endpoint=request.tunnel_endpoint, ca_pem=request.tunnel_ca_pem
        )
        # Orphan healing (RELAY.md Sections 6, 10): close any open game_session
        # rows the relay no longer tracks — sessions a relay crash dropped without
        # a SessionEnd. The registration's active set is authoritative.
        closed = await self._session_sink.close_absent(
            active_session_ids=list(request.active_session_ids),
            ended_at=self._clock.now(),
        )
        _LOG.info(
            "relay registered",
            extra={
                "tunnel_endpoint": request.tunnel_endpoint,
                "orphan_sessions_closed": closed,
            },
        )
        return pb.RegisterResponse(base_domain=self._base_domain)

    async def ResolveJoin(  # noqa: N802 (gRPC-generated method name)
        self, request: pb.ResolveJoinRequest, context: aio.ServicerContext
    ) -> pb.ResolveJoinResponse:
        await self._authenticate(context)
        route = await self._resolver.resolve_slug(request.slug)
        if route is None:
            return pb.ResolveJoinResponse(decision=pb.JOIN_DECISION_NOT_FOUND)
        if not self._is_tunnelable(route):
            return pb.ResolveJoinResponse(
                decision=pb.JOIN_DECISION_STOPPED,
                display_name=route.display_name,
            )
        return await self._resolve_tunnel(route)

    async def ReportSessions(  # noqa: N802 (gRPC-generated method name)
        self, request: pb.ReportSessionsRequest, context: aio.ServicerContext
    ) -> pb.ReportSessionsResponse:
        await self._authenticate(context)
        # Persist each lifecycle event idempotently keyed on the relay-minted
        # session_id (RELAY.md Section 6): a start is an insert-or-fill, an end
        # sets ended_at (upserting a placeholder if its start has not arrived).
        # Events are applied in order so an end-before-start in the same batch
        # still reconciles. Retries are safe because every write is idempotent.
        for event in request.events:
            kind = event.WhichOneof("event")
            if kind == "start":
                await self._session_sink.record_start(_to_session_start(event.start))
            elif kind == "end":
                await self._session_sink.record_end(
                    session_id=event.end.session_id,
                    ended_at=_to_datetime(event.end.ended_at),
                )
        return pb.ReportSessionsResponse()

    def _is_tunnelable(self, route: ServerRoute) -> bool:
        """Whether ``route`` is running on a live worker (TUNNEL vs STOPPED).

        STOPPED covers every non-TUNNEL-but-known case: not observed running, no
        assigned worker, or the assigned worker offline (RELAY.md Section 6).
        Worker liveness is the registry's heartbeat-based authority (FR-WRK-2),
        not ad-hoc state.
        """

        if not route.is_running or route.assigned_worker_id is None:
            return False
        try:
            worker_id = WorkerId(route.assigned_worker_id)
        except InvalidWorkerIdError:
            return False
        worker = self._registry.get(worker_id)
        return worker is not None and worker.status is WorkerStatus.ONLINE

    async def _resolve_tunnel(self, route: ServerRoute) -> pb.ResolveJoinResponse:
        # An assigned, online worker is guaranteed by _is_tunnelable.
        assert route.assigned_worker_id is not None
        worker_id = WorkerId(route.assigned_worker_id)
        registered = self._registration.current()
        if registered is None:
            # No relay has registered its tunnel endpoint, so a TunnelDial would
            # have nothing to carry — the relay cannot complete the join. Treat it
            # as STOPPED so the relay answers in-protocol rather than hanging.
            _LOG.warning("ResolveJoin TUNNEL with no registered relay endpoint")
            return pb.ResolveJoinResponse(
                decision=pb.JOIN_DECISION_STOPPED,
                display_name=route.display_name,
            )
        token = self._token_table.mint()
        try:
            await self._control_plane.dispatch_fire_and_forget(
                worker_id=worker_id,
                server_id=route.server_id,
                command=TunnelDialCommand(
                    endpoint=registered.endpoint,
                    token=token,
                    tls_ca_pem=registered.ca_pem,
                ),
            )
        except WorkerNotConnectedError:
            # The worker dropped between the liveness check and dispatch; the
            # relay answers in-protocol as STOPPED.
            return pb.ResolveJoinResponse(
                decision=pb.JOIN_DECISION_STOPPED,
                display_name=route.display_name,
            )
        return pb.ResolveJoinResponse(
            decision=pb.JOIN_DECISION_TUNNEL,
            token=token,
            server_id=route.server_id,
        )

    async def _authenticate(self, context: aio.ServicerContext) -> None:
        presented = self._credential_from_metadata(context)
        if presented is None or not hmac.compare_digest(presented, self._credential):
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED, "relay credential rejected"
            )

    @staticmethod
    def _credential_from_metadata(context: aio.ServicerContext) -> str | None:
        for key, value in context.invocation_metadata() or ():
            text = str(value)
            if key == _AUTH_METADATA_KEY and text.startswith(_BEARER_PREFIX):
                return text[len(_BEARER_PREFIX) :]
        return None


def _to_datetime(ts: Timestamp) -> dt.datetime:
    """Convert a protobuf ``Timestamp`` to a timezone-aware UTC datetime."""

    return ts.ToDatetime(tzinfo=dt.timezone.utc)


def _to_session_start(start: pb.SessionStart) -> SessionStart:
    """Translate a proto ``SessionStart`` to the fleet Port value.

    Empty proto3 strings (``username`` / ``player_uuid``) mean *absent* — the
    relay omits them when Login Start did not carry them (RELAY.md Section 8).
    """

    return SessionStart(
        session_id=start.session_id,
        server_id=start.server_id,
        hostname=start.slug,
        player_ip=start.player_ip,
        username=start.username or None,
        player_uuid=start.player_uuid or None,
        started_at=_to_datetime(start.started_at),
    )


def register_relay_service(
    server: aio.Server,
    *,
    credential: str,
    base_domain: str,
    registration: RelayRegistration,
    token_table: JoinTokenTable,
    resolver: ServerRouteResolver,
    registry: WorkerRegistry,
    control_plane: GrpcControlPlane,
    session_sink: SessionSink,
    clock: Clock,
) -> None:
    """Add the RelayService servicer to an existing gRPC server (RELAY.md 6).

    Called only when ``relay.enabled``; the relay shares the Worker's listener
    (``server.grpc_port``) so a deployment opens one gRPC port for both.
    """

    servicer = RelayServicer(
        credential=credential,
        base_domain=base_domain,
        registration=registration,
        token_table=token_table,
        resolver=resolver,
        registry=registry,
        control_plane=control_plane,
        session_sink=session_sink,
        clock=clock,
    )
    add_RelayServiceServicer_to_server(servicer, server)
