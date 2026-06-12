"""In-process integration tests for the RelayService gRPC servicer (issue #956).

Starts a real grpc.aio server on an ephemeral localhost port and dials it with a
real RelayService client, so the relay-to-API contract is exercised end to end
(RELAY.md Sections 4, 6) without any database (NFR-TEST-1):

- auth: a wrong/missing relay credential is rejected with UNAUTHENTICATED;
- Register: stores endpoint/CA (last-writer-wins) and returns base_domain;
- ResolveJoin decision matrix: NOT_FOUND / STOPPED (not running / no worker /
  worker offline) / TUNNEL;
- on TUNNEL a single-use token is minted and a TunnelDial is dispatched to the
  assigned worker carrying the registered endpoint/CA;
- ReportSessions responds UNIMPLEMENTED (issue #957).
"""

from __future__ import annotations

import datetime as dt

import grpc
import pytest
from grpc import aio

from mc_server_dashboard_api.fleet.adapters.control_plane import (
    ControlPlaneState,
    GrpcControlPlane,
)
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.adapters.relay_server import register_relay_service
from mc_server_dashboard_api.fleet.adapters.relay_state import (
    JoinTokenTable,
    RelayRegistration,
)
from mc_server_dashboard_api.fleet.domain.server_route_resolver import (
    ServerRoute,
    ServerRouteResolver,
)
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from mcsd.relay.v1 import relay_pb2 as pb
from mcsd.relay.v1.relay_pb2_grpc import RelayServiceStub
from tests.fleet.fakes import FakeClock, make_worker

_T0 = dt.datetime(2026, 6, 12, 12, 0, tzinfo=dt.timezone.utc)
_TIMEOUT = dt.timedelta(seconds=30)
_CREDENTIAL = "shared-relay-secret"
_BASE_DOMAIN = "mc.example.com"
_WORKER_ID = "33333333-3333-3333-3333-333333333333"
_SERVER_ID = "44444444-4444-4444-4444-444444444444"


class FakeResolver(ServerRouteResolver):
    def __init__(self, routes: dict[str, ServerRoute] | None = None) -> None:
        self._routes = routes or {}

    async def resolve_slug(self, slug: str) -> ServerRoute | None:
        return self._routes.get(slug)


def _running_route(
    *, server_id: str = _SERVER_ID, worker_id: str = _WORKER_ID
) -> ServerRoute:
    return ServerRoute(
        server_id=server_id,
        display_name="My Server",
        is_running=True,
        assigned_worker_id=worker_id,
    )


class _Harness:
    def __init__(
        self,
        *,
        resolver: ServerRouteResolver,
        registry: InMemoryWorkerRegistry,
        control_plane: ControlPlaneState,
        registration: RelayRegistration | None = None,
        token_table: JoinTokenTable | None = None,
    ) -> None:
        self.resolver = resolver
        self.registry = registry
        self.control_plane = control_plane
        self.registration = registration or RelayRegistration()
        self.token_table = token_table or JoinTokenTable(
            clock=FakeClock(_T0), ttl=dt.timedelta(seconds=10)
        )
        self._server: aio.Server | None = None
        self._channels: list[aio.Channel] = []

    async def start(self) -> RelayServiceStub:
        server = aio.server()
        register_relay_service(
            server,
            credential=_CREDENTIAL,
            base_domain=_BASE_DOMAIN,
            registration=self.registration,
            token_table=self.token_table,
            resolver=self.resolver,
            registry=self.registry,
            control_plane=GrpcControlPlane(
                self.control_plane, timeout_seconds=_TIMEOUT.total_seconds()
            ),
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        self._server = server
        self._port = port
        return await self.new_stub()

    async def new_stub(self) -> RelayServiceStub:
        channel = aio.insecure_channel(f"127.0.0.1:{self._port}")
        self._channels.append(channel)
        return RelayServiceStub(channel)

    async def stop(self) -> None:
        for channel in self._channels:
            await channel.close()
        if self._server is not None:
            await self._server.stop(grace=None)


def _auth() -> list[tuple[str, str]]:
    return [("authorization", f"Bearer {_CREDENTIAL}")]


@pytest.fixture
def registry() -> InMemoryWorkerRegistry:
    return InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)


def _register_online_worker(registry: InMemoryWorkerRegistry) -> None:
    registry.register(make_worker(worker_id=_WORKER_ID, at=_T0))


async def _make_harness(
    *,
    resolver: ServerRouteResolver,
    registry: InMemoryWorkerRegistry,
    control_plane: ControlPlaneState | None = None,
    registration: RelayRegistration | None = None,
    token_table: JoinTokenTable | None = None,
) -> tuple[_Harness, RelayServiceStub]:
    harness = _Harness(
        resolver=resolver,
        registry=registry,
        control_plane=control_plane or ControlPlaneState(),
        registration=registration,
        token_table=token_table,
    )
    stub = await harness.start()
    return harness, stub


async def test_register_rejects_missing_credential(
    registry: InMemoryWorkerRegistry,
) -> None:
    harness, stub = await _make_harness(resolver=FakeResolver(), registry=registry)
    try:
        with pytest.raises(aio.AioRpcError) as exc:
            await stub.Register(pb.RegisterRequest(tunnel_endpoint="r:1"))
        assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await harness.stop()


async def test_register_rejects_wrong_credential(
    registry: InMemoryWorkerRegistry,
) -> None:
    harness, stub = await _make_harness(resolver=FakeResolver(), registry=registry)
    try:
        with pytest.raises(aio.AioRpcError) as exc:
            await stub.Register(
                pb.RegisterRequest(tunnel_endpoint="r:1"),
                metadata=[("authorization", "Bearer wrong")],
            )
        assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await harness.stop()


async def test_resolve_join_rejects_missing_credential(
    registry: InMemoryWorkerRegistry,
) -> None:
    harness, stub = await _make_harness(resolver=FakeResolver(), registry=registry)
    try:
        with pytest.raises(aio.AioRpcError) as exc:
            await stub.ResolveJoin(pb.ResolveJoinRequest(slug="amber-falcon-42"))
        assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await harness.stop()


async def test_register_returns_base_domain_and_stores_endpoint(
    registry: InMemoryWorkerRegistry,
) -> None:
    registration = RelayRegistration()
    harness, stub = await _make_harness(
        resolver=FakeResolver(), registry=registry, registration=registration
    )
    try:
        resp = await stub.Register(
            pb.RegisterRequest(tunnel_endpoint="relay:25665", tunnel_ca_pem="CA"),
            metadata=_auth(),
        )
        assert resp.base_domain == _BASE_DOMAIN
        stored = registration.current()
        assert stored is not None
        assert stored.endpoint == "relay:25665"
        assert stored.ca_pem == "CA"
    finally:
        await harness.stop()


async def test_register_last_writer_wins(
    registry: InMemoryWorkerRegistry,
) -> None:
    registration = RelayRegistration()
    harness, stub = await _make_harness(
        resolver=FakeResolver(), registry=registry, registration=registration
    )
    try:
        await stub.Register(
            pb.RegisterRequest(tunnel_endpoint="old:1", tunnel_ca_pem="old"),
            metadata=_auth(),
        )
        await stub.Register(
            pb.RegisterRequest(tunnel_endpoint="new:2", tunnel_ca_pem="new"),
            metadata=_auth(),
        )
        stored = registration.current()
        assert stored is not None
        assert stored.endpoint == "new:2"
    finally:
        await harness.stop()


async def test_resolve_join_not_found(
    registry: InMemoryWorkerRegistry,
) -> None:
    harness, stub = await _make_harness(resolver=FakeResolver(), registry=registry)
    try:
        resp = await stub.ResolveJoin(
            pb.ResolveJoinRequest(slug="nope"), metadata=_auth()
        )
        assert resp.decision == pb.JOIN_DECISION_NOT_FOUND
    finally:
        await harness.stop()


async def test_resolve_join_stopped_when_not_running(
    registry: InMemoryWorkerRegistry,
) -> None:
    _register_online_worker(registry)
    route = ServerRoute(
        server_id=_SERVER_ID,
        display_name="My Server",
        is_running=False,
        assigned_worker_id=_WORKER_ID,
    )
    harness, stub = await _make_harness(
        resolver=FakeResolver({"slug": route}), registry=registry
    )
    try:
        resp = await stub.ResolveJoin(
            pb.ResolveJoinRequest(slug="slug"), metadata=_auth()
        )
        assert resp.decision == pb.JOIN_DECISION_STOPPED
        assert resp.display_name == "My Server"
    finally:
        await harness.stop()


async def test_resolve_join_stopped_when_no_worker(
    registry: InMemoryWorkerRegistry,
) -> None:
    route = ServerRoute(
        server_id=_SERVER_ID,
        display_name="My Server",
        is_running=True,
        assigned_worker_id=None,
    )
    harness, stub = await _make_harness(
        resolver=FakeResolver({"slug": route}), registry=registry
    )
    try:
        resp = await stub.ResolveJoin(
            pb.ResolveJoinRequest(slug="slug"), metadata=_auth()
        )
        assert resp.decision == pb.JOIN_DECISION_STOPPED
    finally:
        await harness.stop()


async def test_resolve_join_stopped_when_worker_offline(
    registry: InMemoryWorkerRegistry,
) -> None:
    # The worker has never registered, so the registry has no live session for it.
    harness, stub = await _make_harness(
        resolver=FakeResolver({"slug": _running_route()}), registry=registry
    )
    try:
        resp = await stub.ResolveJoin(
            pb.ResolveJoinRequest(slug="slug"), metadata=_auth()
        )
        assert resp.decision == pb.JOIN_DECISION_STOPPED
    finally:
        await harness.stop()


async def test_resolve_join_tunnel_mints_token_and_dispatches(
    registry: InMemoryWorkerRegistry,
) -> None:
    _register_online_worker(registry)
    control_plane = ControlPlaneState()
    # Open an outbound session for the worker so the fire-and-forget dispatch has
    # a live queue to deliver the TunnelDial onto.
    queue = control_plane.open_session(WorkerId(_WORKER_ID), session=0)
    registration = RelayRegistration()
    registration.set(endpoint="relay:25665", ca_pem="CA-PEM")
    harness, stub = await _make_harness(
        resolver=FakeResolver({"slug": _running_route()}),
        registry=registry,
        control_plane=control_plane,
        registration=registration,
    )
    try:
        resp = await stub.ResolveJoin(
            pb.ResolveJoinRequest(slug="slug", intent=pb.JOIN_INTENT_LOGIN),
            metadata=_auth(),
        )
        assert resp.decision == pb.JOIN_DECISION_TUNNEL
        assert resp.server_id == _SERVER_ID
        assert len(resp.token) == 32
        # The TunnelDial was dispatched to the assigned worker's outbound queue,
        # carrying the registered endpoint/CA and the minted token.
        message = await queue.get()
        dial = message.api_command.tunnel_dial
        assert message.api_command.server_id == _SERVER_ID
        assert dial.server_id == _SERVER_ID
        assert dial.endpoint == "relay:25665"
        assert dial.tls_ca_pem == "CA-PEM"
        assert dial.token == resp.token
    finally:
        await harness.stop()


async def test_resolve_join_token_is_single_use(
    registry: InMemoryWorkerRegistry,
) -> None:
    _register_online_worker(registry)
    control_plane = ControlPlaneState()
    control_plane.open_session(WorkerId(_WORKER_ID), session=0)
    registration = RelayRegistration()
    registration.set(endpoint="relay:25665", ca_pem="CA")
    token_table = JoinTokenTable(clock=FakeClock(_T0), ttl=dt.timedelta(seconds=10))
    harness, stub = await _make_harness(
        resolver=FakeResolver({"slug": _running_route()}),
        registry=registry,
        control_plane=control_plane,
        registration=registration,
        token_table=token_table,
    )
    try:
        resp = await stub.ResolveJoin(
            pb.ResolveJoinRequest(slug="slug"), metadata=_auth()
        )
        # The relay consumes the token on the worker's dial-back; a second consume
        # (a replay) must fail.
        assert token_table.consume(resp.token) == _SERVER_ID
        assert token_table.consume(resp.token) is None
    finally:
        await harness.stop()


async def test_report_sessions_unimplemented(
    registry: InMemoryWorkerRegistry,
) -> None:
    harness, stub = await _make_harness(resolver=FakeResolver(), registry=registry)
    try:
        with pytest.raises(aio.AioRpcError) as exc:
            await stub.ReportSessions(pb.ReportSessionsRequest(), metadata=_auth())
        assert exc.value.code() == grpc.StatusCode.UNIMPLEMENTED
    finally:
        await harness.stop()
