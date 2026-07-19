"""In-process integration tests for the RelayService gRPC servicer (issue #956).

Starts a real grpc.aio server on an ephemeral localhost port and dials it with a
real RelayService client, so the relay-to-API contract is exercised end to end
(RELAY.md Sections 4, 6) without any database (NFR-TEST-1):

- auth: a wrong/missing relay credential is rejected with UNAUTHENTICATED;
- Register: stores endpoint/CA (last-writer-wins) and returns base_domain;
- ResolveJoin decision matrix: NOT_FOUND / STOPPED (not running / no worker /
  worker offline) / TUNNEL;
- on TUNNEL a token is minted and a TunnelDial is dispatched to the assigned
  worker carrying the registered endpoint/CA;
- ReportSessions persists start/end events through the SessionSink (issue #957);
- Register closes orphaned sessions absent from the active set (issue #957).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import grpc
import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from grpc import aio

from mc_server_dashboard_api.fleet.adapters.control_plane import (
    ControlPlaneState,
    GrpcControlPlane,
)
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.adapters.relay_server import register_relay_service
from mc_server_dashboard_api.fleet.adapters.relay_state import (
    BedrockTunnelTable,
    JoinTokenTable,
    RelayRegistration,
)
from mc_server_dashboard_api.fleet.domain.server_route_resolver import (
    ServerRoute,
    ServerRouteResolver,
)
from mc_server_dashboard_api.fleet.domain.session_sink import SessionSink, SessionStart
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from mcsd.relay.v1 import relay_pb2 as pb
from mcsd.relay.v1.relay_pb2_grpc import RelayServiceStub
from tests.fleet.fakes import FakeClock, make_worker


class _RecordingSessionSink(SessionSink):
    """In-memory SessionSink capturing the servicer's calls for assertions."""

    def __init__(self) -> None:
        self.starts: list[SessionStart] = []
        self.ends: list[tuple[str, dt.datetime]] = []
        self.close_absent_calls: list[tuple[list[str], dt.datetime]] = []

    async def record_start(self, start: SessionStart) -> None:
        self.starts.append(start)

    async def record_end(self, *, session_id: str, ended_at: dt.datetime) -> None:
        self.ends.append((session_id, ended_at))

    async def close_absent(
        self, *, active_session_ids: Sequence[str], ended_at: dt.datetime
    ) -> int:
        self.close_absent_calls.append((list(active_session_ids), ended_at))
        return 0


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
        bedrock_tunnel_table: BedrockTunnelTable | None = None,
        session_sink: SessionSink | None = None,
    ) -> None:
        self.resolver = resolver
        self.registry = registry
        self.control_plane = control_plane
        self.registration = registration or RelayRegistration()
        self.token_table = token_table or JoinTokenTable()
        self.bedrock_tunnel_table = bedrock_tunnel_table or BedrockTunnelTable()
        self.session_sink = session_sink or _RecordingSessionSink()
        self.clock = FakeClock(_T0)
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
            bedrock_tunnel_table=self.bedrock_tunnel_table,
            resolver=self.resolver,
            registry=self.registry,
            control_plane=GrpcControlPlane(
                self.control_plane,
                clock=self.clock,
                timeout_seconds=_TIMEOUT.total_seconds(),
            ),
            session_sink=self.session_sink,
            clock=self.clock,
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
    bedrock_tunnel_table: BedrockTunnelTable | None = None,
    session_sink: SessionSink | None = None,
) -> tuple[_Harness, RelayServiceStub]:
    harness = _Harness(
        resolver=resolver,
        registry=registry,
        control_plane=control_plane or ControlPlaneState(),
        registration=registration,
        token_table=token_table,
        bedrock_tunnel_table=bedrock_tunnel_table,
        session_sink=session_sink,
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


async def test_resolve_join_stopped_when_no_relay_registered(
    registry: InMemoryWorkerRegistry,
) -> None:
    # The route is TUNNEL-eligible (running, online worker) but no relay has
    # registered its tunnel endpoint, so a TunnelDial would carry nothing — the
    # servicer answers STOPPED rather than hanging (relay_server.py STOPPED arm).
    _register_online_worker(registry)
    control_plane = ControlPlaneState()
    control_plane.open_session(WorkerId(_WORKER_ID), session=0)
    harness, stub = await _make_harness(
        resolver=FakeResolver({"slug": _running_route()}),
        registry=registry,
        control_plane=control_plane,
        registration=RelayRegistration(),  # never .set(): no registration
    )
    try:
        resp = await stub.ResolveJoin(
            pb.ResolveJoinRequest(slug="slug"), metadata=_auth()
        )
        assert resp.decision == pb.JOIN_DECISION_STOPPED
        assert resp.display_name == "My Server"
    finally:
        await harness.stop()


async def test_resolve_join_stopped_when_worker_not_connected(
    registry: InMemoryWorkerRegistry,
) -> None:
    # The worker is online in the registry (passes the liveness check) but has no
    # open outbound session, so dispatch raises WorkerNotConnectedError — the
    # servicer answers STOPPED in-protocol (relay_server.py WorkerNotConnected arm).
    _register_online_worker(registry)
    registration = RelayRegistration()
    registration.set(endpoint="relay:25665", ca_pem="CA")
    harness, stub = await _make_harness(
        resolver=FakeResolver({"slug": _running_route()}),
        registry=registry,
        control_plane=ControlPlaneState(),  # no open_session for the worker
        registration=registration,
    )
    try:
        resp = await stub.ResolveJoin(
            pb.ResolveJoinRequest(slug="slug"), metadata=_auth()
        )
        assert resp.decision == pb.JOIN_DECISION_STOPPED
        assert resp.display_name == "My Server"
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


async def test_resolve_join_status_intent_dispatches_tunnel_dial(
    registry: InMemoryWorkerRegistry,
) -> None:
    # STATUS pings on cache miss still need a TunnelDial so the relay can fetch
    # the real Minecraft status through the tunnel (RELAY.md Section 7). The API
    # dispatches TunnelDial for both STATUS and LOGIN intents on running servers.
    _register_online_worker(registry)
    control_plane = ControlPlaneState()
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
            pb.ResolveJoinRequest(slug="slug", intent=pb.JOIN_INTENT_STATUS),
            metadata=_auth(),
        )
        assert resp.decision == pb.JOIN_DECISION_TUNNEL
        assert resp.server_id == _SERVER_ID
        assert len(resp.token) == 32
        # TunnelDial dispatched even for STATUS intent — the relay needs the
        # tunnel to fetch real status data on cache miss.
        message = await queue.get()
        dial = message.api_command.tunnel_dial
        assert dial.endpoint == "relay:25665"
        assert dial.token == resp.token
    finally:
        await harness.stop()


async def test_report_sessions_rejects_missing_credential(
    registry: InMemoryWorkerRegistry,
) -> None:
    harness, stub = await _make_harness(resolver=FakeResolver(), registry=registry)
    try:
        with pytest.raises(aio.AioRpcError) as exc:
            await stub.ReportSessions(pb.ReportSessionsRequest())
        assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await harness.stop()


def _timestamp(at: dt.datetime) -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(at)
    return ts


_SESSION_ID = "55555555-5555-5555-5555-555555555555"


async def test_report_sessions_persists_start_and_end(
    registry: InMemoryWorkerRegistry,
) -> None:
    sink = _RecordingSessionSink()
    harness, stub = await _make_harness(
        resolver=FakeResolver(), registry=registry, session_sink=sink
    )
    try:
        await stub.ReportSessions(
            pb.ReportSessionsRequest(
                events=[
                    pb.SessionEvent(
                        start=pb.SessionStart(
                            session_id=_SESSION_ID,
                            server_id=_SERVER_ID,
                            slug="amber-falcon-42",
                            player_ip="203.0.113.7",
                            username="steve",
                            player_uuid="66666666-6666-6666-6666-666666666666",
                            started_at=_timestamp(_T0),
                            source=pb.SESSION_SOURCE_JAVA,
                        )
                    ),
                    pb.SessionEvent(
                        end=pb.SessionEnd(
                            session_id=_SESSION_ID,
                            ended_at=_timestamp(_T0 + dt.timedelta(minutes=5)),
                        )
                    ),
                ]
            ),
            metadata=_auth(),
        )
        assert len(sink.starts) == 1
        start = sink.starts[0]
        assert start.session_id == _SESSION_ID
        assert start.server_id == _SERVER_ID
        assert start.hostname == "amber-falcon-42"
        assert start.player_ip == "203.0.113.7"
        assert start.username == "steve"
        assert start.player_uuid == "66666666-6666-6666-6666-666666666666"
        assert start.started_at == _T0
        assert start.source == "java"
        assert sink.ends == [(_SESSION_ID, _T0 + dt.timedelta(minutes=5))]
    finally:
        await harness.stop()


async def test_report_sessions_omits_blank_claimed_identity(
    registry: InMemoryWorkerRegistry,
) -> None:
    # An empty proto3 string for username/player_uuid means absent (Login Start
    # did not carry it) — the seam maps it to None, not "".
    sink = _RecordingSessionSink()
    harness, stub = await _make_harness(
        resolver=FakeResolver(), registry=registry, session_sink=sink
    )
    try:
        await stub.ReportSessions(
            pb.ReportSessionsRequest(
                events=[
                    pb.SessionEvent(
                        start=pb.SessionStart(
                            session_id=_SESSION_ID,
                            server_id=_SERVER_ID,
                            slug="amber-falcon-42",
                            player_ip="203.0.113.7",
                            started_at=_timestamp(_T0),
                        )
                    )
                ]
            ),
            metadata=_auth(),
        )
        assert sink.starts[0].username is None
        assert sink.starts[0].player_uuid is None
        # An unset proto source (SESSION_SOURCE_UNSPECIFIED, e.g. an older relay)
        # maps to None — stored as the legacy/unspecified source (issue #1912).
        assert sink.starts[0].source is None
    finally:
        await harness.stop()


async def test_report_sessions_maps_bedrock_source(
    registry: InMemoryWorkerRegistry,
) -> None:
    # A Bedrock flow-session (SESSION_SOURCE_BEDROCK) is threaded to the sink as
    # "bedrock" so the history can label it honestly (issue #1912).
    sink = _RecordingSessionSink()
    harness, stub = await _make_harness(
        resolver=FakeResolver(), registry=registry, session_sink=sink
    )
    try:
        await stub.ReportSessions(
            pb.ReportSessionsRequest(
                events=[
                    pb.SessionEvent(
                        start=pb.SessionStart(
                            session_id=_SESSION_ID,
                            server_id=_SERVER_ID,
                            slug="amber-falcon-42",
                            player_ip="203.0.113.7",
                            started_at=_timestamp(_T0),
                            source=pb.SESSION_SOURCE_BEDROCK,
                        )
                    )
                ]
            ),
            metadata=_auth(),
        )
        assert sink.starts[0].source == "bedrock"
    finally:
        await harness.stop()


async def test_validate_bedrock_tunnel_rejects_missing_credential(
    registry: InMemoryWorkerRegistry,
) -> None:
    harness, stub = await _make_harness(resolver=FakeResolver(), registry=registry)
    try:
        with pytest.raises(aio.AioRpcError) as exc:
            await stub.ValidateBedrockTunnel(
                pb.ValidateBedrockTunnelRequest(
                    server_id=_SERVER_ID, bedrock_port=19132, token="tok"
                )
            )
        assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await harness.stop()


async def test_validate_bedrock_tunnel_matches_open_credential(
    registry: InMemoryWorkerRegistry,
) -> None:
    table = BedrockTunnelTable()
    token = table.open(server_id=_SERVER_ID, bedrock_port=19132)
    harness, stub = await _make_harness(
        resolver=FakeResolver(), registry=registry, bedrock_tunnel_table=table
    )
    try:
        resp = await stub.ValidateBedrockTunnel(
            pb.ValidateBedrockTunnelRequest(
                server_id=_SERVER_ID, bedrock_port=19132, token=token
            ),
            metadata=_auth(),
        )
        assert resp.valid is True
    finally:
        await harness.stop()


async def test_validate_bedrock_tunnel_rejects_wrong_token(
    registry: InMemoryWorkerRegistry,
) -> None:
    table = BedrockTunnelTable()
    table.open(server_id=_SERVER_ID, bedrock_port=19132)
    harness, stub = await _make_harness(
        resolver=FakeResolver(), registry=registry, bedrock_tunnel_table=table
    )
    try:
        resp = await stub.ValidateBedrockTunnel(
            pb.ValidateBedrockTunnelRequest(
                server_id=_SERVER_ID, bedrock_port=19132, token="wrong"
            ),
            metadata=_auth(),
        )
        assert resp.valid is False
    finally:
        await harness.stop()


async def test_validate_bedrock_tunnel_rejects_after_close(
    registry: InMemoryWorkerRegistry,
) -> None:
    table = BedrockTunnelTable()
    token = table.open(server_id=_SERVER_ID, bedrock_port=19132)
    table.close(server_id=_SERVER_ID)
    harness, stub = await _make_harness(
        resolver=FakeResolver(), registry=registry, bedrock_tunnel_table=table
    )
    try:
        resp = await stub.ValidateBedrockTunnel(
            pb.ValidateBedrockTunnelRequest(
                server_id=_SERVER_ID, bedrock_port=19132, token=token
            ),
            metadata=_auth(),
        )
        assert resp.valid is False
    finally:
        await harness.stop()


async def test_register_closes_orphaned_sessions(
    registry: InMemoryWorkerRegistry,
) -> None:
    # Register carries the relay's still-active set; the servicer asks the sink to
    # close every open row absent from it (orphan healing, RELAY.md Sections 6, 10).
    sink = _RecordingSessionSink()
    harness, stub = await _make_harness(
        resolver=FakeResolver(), registry=registry, session_sink=sink
    )
    try:
        await stub.Register(
            pb.RegisterRequest(
                tunnel_endpoint="relay:25665",
                active_session_ids=[_SESSION_ID],
            ),
            metadata=_auth(),
        )
        assert sink.close_absent_calls == [([_SESSION_ID], _T0)]
    finally:
        await harness.stop()
