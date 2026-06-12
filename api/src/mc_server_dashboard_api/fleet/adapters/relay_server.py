"""The ``RelayService`` gRPC servicer (RELAY.md Sections 4, 6; issue #956).

Served on the API's existing gRPC listener (``server.grpc_port``) alongside
``WorkerService``, only when ``relay.enabled``. Transport posture mirrors the
Worker (CONTROL_PLANE.md Section 2): server-side TLS plus a shared-secret
credential in call metadata — but a *separate* credential (``relay.credential``)
so relay and Worker credentials rotate independently. All three RPCs are unary
and relay-initiated; the relay has no command inbox.

- ``Register`` stores the relay's tunnel endpoint + CA PEM (last-writer-wins, one
  relay per deployment) and returns ``base_domain``.
- ``ResolveJoin`` maps a slug to a routing decision (NOT_FOUND / STOPPED /
  TUNNEL). On TUNNEL it mints a single-use token, dispatches a ``TunnelDial`` to
  the assigned Worker fire-and-forget, and returns ``{TUNNEL, token, server_id}``.
- ``ReportSessions`` is UNIMPLEMENTED here — game-session persistence is issue
  #957; the RPC responds cleanly rather than crashing.

Only this module (and the wiring) touches grpcio / the generated stubs; the
domain stays transport-free (ARCHITECTURE.md Section 2.1).
"""

from __future__ import annotations

import hmac
import logging

import grpc
from grpc import aio

from mc_server_dashboard_api.fleet.adapters.control_plane import GrpcControlPlane
from mc_server_dashboard_api.fleet.adapters.relay_state import (
    JoinTokenTable,
    RelayRegistration,
)
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
    ) -> None:
        self._credential = credential
        self._base_domain = base_domain
        self._registration = registration
        self._token_table = token_table
        self._resolver = resolver
        self._registry = registry
        self._control_plane = control_plane

    async def Register(  # noqa: N802 (gRPC-generated method name)
        self, request: pb.RegisterRequest, context: aio.ServicerContext
    ) -> pb.RegisterResponse:
        await self._authenticate(context)
        self._registration.set(
            endpoint=request.tunnel_endpoint, ca_pem=request.tunnel_ca_pem
        )
        _LOG.info(
            "relay registered",
            extra={"tunnel_endpoint": request.tunnel_endpoint},
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
        # game_session persistence (and orphan healing on Register) is issue #957;
        # respond cleanly rather than leaving the RPC crashing.
        await context.abort(
            grpc.StatusCode.UNIMPLEMENTED,
            "ReportSessions is not implemented yet (issue #957)",
        )
        raise AssertionError("unreachable: context.abort raises")  # pragma: no cover

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
        token = self._token_table.mint(server_id=route.server_id)
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
    )
    add_RelayServiceServicer_to_server(servicer, server)
