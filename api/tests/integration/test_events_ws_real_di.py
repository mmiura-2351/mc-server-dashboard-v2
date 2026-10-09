"""De-masking gate for the WebSocket events DI graph (issue #509).

The TestClient unit suites for the events endpoints override
``get_membership_visibility`` / ``get_permission_checker`` / ``get_read_server``
with *no-arg* fakes, so FastAPI never tries to inject the real dependencies on a
WebSocket route. That masked a 500-at-handshake bug: those factories declared a
``Request`` parameter, which FastAPI cannot inject on a WebSocket route (a WS
route gets a ``WebSocket``, not a ``Request``), so the real handshake raised
``missing 1 required positional argument: 'request'``.

These tests exercise the *real* dependency graph over a real socket (TestClient
WebSocket connect) against a real database, overriding only true externals — the
authenticated user behind the handshake and the in-process event bus. A
``Request``-param regression on any dependency in the WS routes' Depends graph
makes the handshake fail to resolve and these tests fail again.

The same real graph carries the snapshot-ordering regression of issue #3212:
a worker that reports and then disconnects while the relay's snapshot read is
in flight, with both writes going through the real state sink.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.websockets import WebSocketDisconnect

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import (
    Community,
    Membership,
    Role,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    CommunityName,
    MembershipId,
    Permission,
    RoleId,
    RoleName,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    UserId as CommunityUserId,
)
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.dependencies import (
    get_current_user_ws,
    get_list_servers,
    get_read_server,
    get_real_time_events,
    get_server_community_lookup,
)
from mc_server_dashboard_api.fleet.adapters.real_time_events import (
    InProcessRealTimeEvents,
)
from mc_server_dashboard_api.fleet.domain.real_time_events import (
    EventStream,
    RealTimeEvent,
    notification_event,
)
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.lifecycle import StartServer
from mc_server_dashboard_api.servers.application.manage_server import (
    CreateServer,
    ListServers,
    ReadServer,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId as ServersCommunityId,
)
from mc_server_dashboard_api.servers.domain.value_objects import WorkerId
from tests.client_utils import enter_client
from tests.identity.fakes import make_authentication, make_user
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import (
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeStoreGenerationReader,
    FakeVersionValidator,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_SERVER_READ = Permission("server:read")
_CLOSE_NOT_FOUND = 4404


@pytest.fixture
async def _database(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """Point the app at the real test DB and bring the schema to head.

    The app factory's lifespan builds the engine from ``MCD_API_DATABASE__URL``,
    so overriding it here makes the real DI graph run against this database (the
    autouse dummy-URL fixture is overridden for this test only).
    """

    assert _DB_URL is not None
    monkeypatch.setenv("MCD_API_DATABASE__URL", _DB_URL)
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    try:
        yield _DB_URL
    finally:
        await downgrade_base(_DB_URL)


async def _seed_member(db_url: str, user_id: uuid.UUID) -> CommunityId:
    """Insert a community and make ``user_id`` a member with ``server:read``."""

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    'INSERT INTO "user" '
                    "(id, username, email, password_hash, is_platform_admin, "
                    "created_at, updated_at) VALUES "
                    "(:id, 'alice', 'alice@e.com', 'h', false, now(), now())"
                ),
                {"id": user_id},
            )
        factory = create_session_factory(engine)
        community = Community(
            id=CommunityId.new(),
            name=CommunityName("guild"),
            created_at=_NOW,
            updated_at=_NOW,
        )
        role = Role(
            id=RoleId.new(),
            community_id=community.id,
            name=RoleName("Op"),
            permissions={_SERVER_READ},
            created_at=_NOW,
            updated_at=_NOW,
        )
        membership = Membership(
            id=MembershipId.new(),
            user_id=CommunityUserId(user_id),
            community_id=community.id,
            created_at=_NOW,
        )
        async with CommunityUnitOfWork(factory) as uow:
            await uow.communities.add(community)
            await uow.roles.add(role)
            await uow.memberships.add(membership)
            await uow.commit()
        async with CommunityUnitOfWork(factory) as uow:
            await uow.memberships.assign_role(membership.id, role.id)
            await uow.commit()
        return community.id
    finally:
        await engine.dispose()


class _FakeLookup:
    """Maps a worker-reported server id (str) to its owning community."""

    def __init__(self, mapping: dict[str, uuid.UUID]) -> None:
        self._mapping = mapping

    async def __call__(self, *, server_id: str) -> uuid.UUID | None:
        return self._mapping.get(server_id)


def _app(
    user: User,
    bus: InProcessRealTimeEvents,
    lookup: dict[str, uuid.UUID] | None = None,
) -> object:
    """Build the app with ONLY true externals overridden (user + bus + lookup).

    The *authorization* graph (``get_membership_visibility`` /
    ``get_permission_checker`` / ``get_read_server``) — the dependencies that
    declared a ``Request`` param and broke WS injection — is left as the real
    factories, so the WebSocket handshake exercises the genuine DI graph. The
    user, the event bus, and the server->community lookup are true externals to
    that graph (the lookup is already a ``WebSocket``-native dependency, not part
    of the bug), so they are faked to keep the test off gRPC and server seeding.
    """

    app = create_app()
    app.dependency_overrides[get_current_user_ws] = lambda: make_authentication(user)
    app.dependency_overrides[get_real_time_events] = lambda: bus
    app.dependency_overrides[get_server_community_lookup] = lambda: _FakeLookup(
        lookup or {}
    )
    return app


def _client(app: object) -> TestClient:
    return enter_client(TestClient(app))  # type: ignore[arg-type]


async def test_community_events_real_graph_accepts_and_delivers(
    _database: str,
) -> None:
    user = make_user()
    community = await _seed_member(_database, user.id.value)
    bus = InProcessRealTimeEvents()
    server = uuid.uuid4()
    client = _client(_app(user, bus, lookup={str(server): community.value}))
    url = f"/api/communities/{community.value}/events"
    with client.websocket_connect(url) as ws:
        # The on-subscribe snapshot (#1795) is read through the real
        # ``get_list_servers`` -- the community has no servers yet.
        snapshot = ws.receive_json()
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(
                stream=EventStream.STATUS, payload={"state": "running"}
            ),
        )
        frame = ws.receive_json()
    assert snapshot["stream"] == "snapshot"
    assert snapshot["payload"] == {"servers": []}
    assert frame["stream"] == "status"
    assert frame["server_id"] == str(server)


async def test_server_events_real_graph_unknown_server_closes_4404(
    _database: str,
) -> None:
    # The per-server route's graph adds the real ``get_read_server``; an unknown
    # server is the documented 4404 close (no cross-community existence signal).
    # Reaching that close proves the whole graph injected on the WS route.
    user = make_user()
    community = await _seed_member(_database, user.id.value)
    bus = InProcessRealTimeEvents()
    server = uuid.uuid4()
    client = _client(_app(user, bus))
    url = f"/api/communities/{community.value}/servers/{server}/events"
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url):
            pass
    assert exc.value.code == _CLOSE_NOT_FOUND


# --- snapshot ordering over real observed-state writes (issue #3212) ---------


async def _seed_started_server(
    db_url: str, community: CommunityId, worker: uuid.UUID
) -> uuid.UUID:
    """Create a server in ``community`` and start it on ``worker``."""

    engine = create_async_engine(db_url)
    try:
        servers_community = ServersCommunityId(community.value)
        server = await CreateServer(
            uow=ServersUnitOfWork(create_session_factory(engine)),
            clock=FakeClock(_NOW),
            version_validator=FakeVersionValidator(),
            file_store=FakeFileStore(),
            port_range=PortRange(start=25565, end=25664),
        )(
            community_id=servers_community,
            name="survival",
            mc_edition="java",
            mc_version="1.21.1",
            server_type="vanilla",
            config={},
        )
        await StartServer(
            uow=ServersUnitOfWork(create_session_factory(engine)),
            control_plane=FakeControlPlane(place_to=WorkerId(worker)),
            clock=FakeClock(_NOW),
            jar_provisioner=FakeJarProvisioner(),
            store_generation=FakeStoreGenerationReader(),
            file_store=FakeFileStore(seed_eula=True),
        )(community_id=servers_community, server_id=server.id)
        return server.id.value
    finally:
        await engine.dispose()


class _WorkerDropsDuringSnapshotRead:
    """The real read use case, raced by a worker that reports then disconnects.

    On the relay's snapshot read, and before that read reaches the database, the
    worker reports ``running`` and its session then drops -- both through the
    real state sink, publishing onto the relay's bus. The ``running`` frame
    therefore enters the subscriber's buffer while the snapshot read is in
    flight, and the disconnect's ``unknown`` is committed before the read: the
    snapshot shows ``unknown`` and the older ``running`` is delivered after it.
    A closing notification marks the end of the race for the test to read up to.

    Runs on the app's event loop (the relay awaits it), with its own engine.
    """

    def __init__(
        self,
        use_case: type[ReadServer] | type[ListServers],
        *,
        db_url: str,
        bus: InProcessRealTimeEvents,
        server: uuid.UUID,
        worker: uuid.UUID,
        snapshot_read: int,
    ) -> None:
        self._use_case = use_case
        self._db_url = db_url
        self._bus = bus
        self._server = str(server)
        self._worker = str(worker)
        self._snapshot_read = snapshot_read
        self._reads = 0

    async def __call__(self, **kwargs: Any) -> Any:
        self._reads += 1
        engine = create_async_engine(self._db_url)
        try:
            factory = create_session_factory(engine)
            if self._reads == self._snapshot_read:
                sink = ServersServerStateSink(
                    factory,
                    clock=FakeClock(_NOW + dt.timedelta(hours=1)),
                    real_time_events=self._bus,
                )
                assert await sink.record_observed_state(
                    server_id=self._server, worker_id=self._worker, state="running"
                )
                await sink.mark_worker_servers_unknown(worker_id=self._worker)
                self._bus.publish(
                    server_id=self._server,
                    event=notification_event(kind="race_over", title="race over"),
                )
            return await self._use_case(uow=ServersUnitOfWork(factory))(**kwargs)
        finally:
            await engine.dispose()


def _states_until_race_over(ws: Any, server: uuid.UUID) -> list[object]:
    """The state each status/snapshot frame sets for ``server``, in order."""

    states: list[object] = []
    while True:
        frame = ws.receive_json()
        if frame["stream"] == "notification":
            return states
        payload = frame["payload"]
        if "servers" in payload:  # the community snapshot
            states.extend(
                entry["state"]
                for entry in payload["servers"]
                if entry["server_id"] == str(server)
            )
        else:
            states.append(payload["state"])


async def test_server_stream_converges_when_the_worker_drops_mid_snapshot_read(
    _database: str,
) -> None:
    """A stale ``running`` delivered after the snapshot is corrected live.

    The disconnect's ``unknown`` used to commit without publishing, so the
    client was left on the older ``running`` until its next snapshot. It now
    publishes after its commit, and the stream itself converges.
    """

    user = make_user()
    community = await _seed_member(_database, user.id.value)
    worker = uuid.uuid4()
    server = await _seed_started_server(_database, community, worker)
    bus = InProcessRealTimeEvents()
    app = _app(user, bus)
    read_server = _WorkerDropsDuringSnapshotRead(
        ReadServer,
        db_url=_database,
        bus=bus,
        server=server,
        worker=worker,
        snapshot_read=2,  # the first read is the accept-time authorization gate
    )
    app.dependency_overrides[get_read_server] = lambda: read_server  # type: ignore[attr-defined]
    url = f"/api/communities/{community.value}/servers/{server}/events"
    with _client(app).websocket_connect(url) as ws:
        states = _states_until_race_over(ws, server)
    assert states == ["unknown", "running", "unknown"]


async def test_community_stream_converges_when_the_worker_drops_mid_snapshot_read(
    _database: str,
) -> None:
    user = make_user()
    community = await _seed_member(_database, user.id.value)
    worker = uuid.uuid4()
    server = await _seed_started_server(_database, community, worker)
    bus = InProcessRealTimeEvents()
    app = _app(user, bus, lookup={str(server): community.value})
    list_servers = _WorkerDropsDuringSnapshotRead(
        ListServers,
        db_url=_database,
        bus=bus,
        server=server,
        worker=worker,
        snapshot_read=1,
    )
    app.dependency_overrides[get_list_servers] = lambda: list_servers  # type: ignore[attr-defined]
    url = f"/api/communities/{community.value}/events"
    with _client(app).websocket_connect(url) as ws:
        states = _states_until_race_over(ws, server)
    assert states == ["unknown", "running", "unknown"]
