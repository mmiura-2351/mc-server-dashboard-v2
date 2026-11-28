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
import functools
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
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
    _build_token_service,
    get_audit_recorder,
    get_current_user,
    get_current_user_ws,
    get_list_servers,
    get_read_server,
    get_real_time_events,
    get_server_community_lookup,
    get_servers_backup_store,
    get_servers_file_store,
    get_version_catalog,
)
from mc_server_dashboard_api.fleet.adapters.real_time_events import (
    InProcessRealTimeEvents,
)
from mc_server_dashboard_api.fleet.api import events as events_module
from mc_server_dashboard_api.fleet.domain.real_time_events import (
    EventStream,
    RealTimeEvent,
    notification_event,
)
from mc_server_dashboard_api.identity.adapters.clock import SystemClock
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.lifecycle import (
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.application.manage_server import (
    CreateServer,
    ListServers,
    ReadServer,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId as ServersCommunityId,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId, WorkerId
from mc_server_dashboard_api.versions.domain.catalog import VersionCatalog
from mc_server_dashboard_api.versions.domain.value_objects import (
    JarSource,
    VersionRef,
)
from mc_server_dashboard_api.versions.domain.value_objects import (
    ServerType as CatalogServerType,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.client_utils import enter_client
from tests.identity.fakes import make_authentication, make_user
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import (
    FakeBackupArchiveStore,
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


def _publish(
    client: TestClient,
    bus: InProcessRealTimeEvents,
    *,
    server_id: str,
    event: RealTimeEvent,
) -> None:
    """Publish ``event`` on the app's event loop, as the real publishers do.

    The TestClient runs the app on its portal's loop in another thread, and the
    bus's delivery is bound to that loop (``asyncio.Event``): a publish from the
    test thread neither wakes the relay nor is safe against it.
    """

    assert client.portal is not None
    client.portal.call(functools.partial(bus.publish, server_id=server_id, event=event))


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
        _publish(
            client,
            bus,
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
            uow=ServersUnitOfWork(
                create_session_factory(engine), InProcessRealTimeEvents()
            ),
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
                    server_id=self._server,
                    worker_id=self._worker,
                    state="running",
                    observed_at=_NOW + dt.timedelta(hours=1),
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


# --- the desired state over the real graph (issue #3428) ---------------------


class _StopDuringSnapshotRead:
    """The real read use case, raced by a stop committed from elsewhere.

    On the relay's snapshot read, and before that read reaches the database, a
    real StopServer commits ``desired=stopped`` through a unit of work wired to
    the relay's bus; its Worker never answers, so the intent is the stop's only
    write. The snapshot then reads the new intent, and the intent's own frame,
    published during the read, is delivered after it. A closing notification
    marks the end of the race for the test to read up to.
    """

    def __init__(
        self,
        use_case: type[ReadServer] | type[ListServers],
        *,
        db_url: str,
        bus: InProcessRealTimeEvents,
        community: CommunityId,
        server: uuid.UUID,
        snapshot_read: int,
    ) -> None:
        self._use_case = use_case
        self._db_url = db_url
        self._bus = bus
        self._community = ServersCommunityId(community.value)
        self._server = server
        self._snapshot_read = snapshot_read
        self._reads = 0

    async def __call__(self, **kwargs: Any) -> Any:
        self._reads += 1
        engine = create_async_engine(self._db_url)
        try:
            factory = create_session_factory(engine)
            if self._reads == self._snapshot_read:
                with pytest.raises(WorkerUnavailableError):
                    await StopServer(
                        uow=ServersUnitOfWork(factory, self._bus),
                        control_plane=FakeControlPlane(unavailable_kinds={"stop"}),
                        clock=FakeClock(_NOW),
                    )(community_id=self._community, server_id=ServerId(self._server))
                self._bus.publish(
                    server_id=str(self._server),
                    event=notification_event(kind="race_over", title="race over"),
                )
            return await self._use_case(uow=ServersUnitOfWork(factory))(**kwargs)
        finally:
            await engine.dispose()


def _frames_until_race_over(ws: Any) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while (frame := ws.receive_json())["stream"] != "notification":
        frames.append(frame)
    return frames


async def test_server_stream_carries_the_desired_state_snapshot_and_change(
    _database: str,
) -> None:
    user = make_user()
    community = await _seed_member(_database, user.id.value)
    worker = uuid.uuid4()
    server = await _seed_started_server(_database, community, worker)
    bus = InProcessRealTimeEvents()
    app = _app(user, bus)
    read_server = _StopDuringSnapshotRead(
        ReadServer,
        db_url=_database,
        bus=bus,
        community=community,
        server=server,
        snapshot_read=2,  # the first read is the accept-time authorization gate
    )
    app.dependency_overrides[get_read_server] = lambda: read_server  # type: ignore[attr-defined]
    url = f"/api/communities/{community.value}/servers/{server}/events"
    with _client(app).websocket_connect(url) as ws:
        frames = _frames_until_race_over(ws)
    # The stop never reached the Worker, so the server is still assigned to it.
    assert [(frame["stream"], frame["payload"]) for frame in frames] == [
        (
            "snapshot",
            {
                "state": "stopped",
                "desired_state": "stopped",
                "assigned_worker_id": str(worker),
                "crash_reason": None,
                "crash_detail": None,
                "crash_not_restarted": False,
                "final_snapshot_refusal": None,
            },
        ),
        ("status", {"desired_state": "stopped"}),
    ]


async def test_community_stream_carries_the_desired_state_snapshot_and_change(
    _database: str,
) -> None:
    user = make_user()
    community = await _seed_member(_database, user.id.value)
    worker = uuid.uuid4()
    server = await _seed_started_server(_database, community, worker)
    bus = InProcessRealTimeEvents()
    app = _app(user, bus, lookup={str(server): community.value})
    list_servers = _StopDuringSnapshotRead(
        ListServers,
        db_url=_database,
        bus=bus,
        community=community,
        server=server,
        snapshot_read=1,
    )
    app.dependency_overrides[get_list_servers] = lambda: list_servers  # type: ignore[attr-defined]
    url = f"/api/communities/{community.value}/events"
    with _client(app).websocket_connect(url) as ws:
        frames = _frames_until_race_over(ws)
    assert [
        (frame["stream"], frame["server_id"], frame["payload"]) for frame in frames
    ] == [
        (
            "snapshot",
            None,
            {
                "servers": [
                    {
                        "server_id": str(server),
                        "state": "stopped",
                        "desired_state": "stopped",
                        "assigned_worker_id": str(worker),
                        "crash_reason": None,
                        "crash_detail": None,
                        "crash_not_restarted": False,
                        "final_snapshot_refusal": None,
                    }
                ]
            },
        ),
        ("status", str(server), {"desired_state": "stopped"}),
    ]


# --- the community's server set over the real graph (issue #3308) ------------


async def _seed_operator(
    db_url: str, community: CommunityId, user_id: uuid.UUID
) -> None:
    """Make ``user_id`` a second member of ``community``, one who creates and
    deletes servers."""

    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    'INSERT INTO "user" '
                    "(id, username, email, password_hash, is_platform_admin, "
                    "created_at, updated_at) VALUES "
                    "(:id, 'bob', 'bob@e.com', 'h', false, now(), now())"
                ),
                {"id": user_id},
            )
        factory = create_session_factory(engine)
        role = Role(
            id=RoleId.new(),
            community_id=community,
            name=RoleName("Builder"),
            permissions={Permission("server:create"), Permission("server:delete")},
            created_at=_NOW,
            updated_at=_NOW,
        )
        membership = Membership(
            id=MembershipId.new(),
            user_id=CommunityUserId(user_id),
            community_id=community,
            created_at=_NOW,
        )
        async with CommunityUnitOfWork(factory) as uow:
            await uow.roles.add(role)
            await uow.memberships.add(membership)
            await uow.commit()
        async with CommunityUnitOfWork(factory) as uow:
            await uow.memberships.assign_role(membership.id, role.id)
            await uow.commit()
    finally:
        await engine.dispose()


class _OneVersionCatalog(VersionCatalog):
    """A catalog that offers every type at ``1.21.1`` and resolves nothing."""

    async def list_versions(self, server_type: CatalogServerType) -> list[VersionRef]:
        return [VersionRef(server_type=server_type, version="1.21.1")]

    async def resolve(self, server_type: CatalogServerType, version: str) -> JarSource:
        raise NotImplementedError


async def test_community_stream_carries_servers_another_member_creates_and_deletes(
    _database: str,
) -> None:
    """A dashboard sees another member's create and delete without a reload.

    Both go through the REST routes and the composition root's units of work,
    and the socket reads the app's own bus through the real membership lookup.
    The deleted server existed before the socket connected and no frame named
    it, so the connection had not learned its community before its row was
    gone.
    """

    watcher = make_user()
    community = await _seed_member(_database, watcher.id.value)
    operator = make_user(username="bob", email="bob@example.com")
    await _seed_operator(_database, community, operator.id.value)
    app = create_app()
    app.dependency_overrides[get_current_user_ws] = lambda: make_authentication(watcher)
    app.dependency_overrides[get_current_user] = lambda: operator
    app.dependency_overrides[get_audit_recorder] = RecordingAuditRecorder
    app.dependency_overrides[get_version_catalog] = _OneVersionCatalog
    app.dependency_overrides[get_servers_file_store] = FakeFileStore
    app.dependency_overrides[get_servers_backup_store] = FakeBackupArchiveStore
    client = _client(app)
    servers = f"/api/communities/{community.value}/servers"
    body = {
        "mc_edition": "java",
        "mc_version": "1.21.1",
        "server_type": "vanilla",
        "config": {},
    }
    doomed = client.post(servers, json={**body, "name": "old"})
    assert doomed.status_code == 201, doomed.text
    doomed_id = doomed.json()["id"]
    with client.websocket_connect(f"/api/communities/{community.value}/events") as ws:
        assert ws.receive_json()["stream"] == "snapshot"
        created = client.post(servers, json={**body, "name": "new"})
        assert created.status_code == 201, created.text
        created_id = created.json()["id"]
        deleted = client.delete(f"{servers}/{doomed_id}")
        assert deleted.status_code == 204, deleted.text
        _publish(
            client,
            app.state.real_time_events,
            server_id=created_id,
            event=notification_event(kind="race_over", title="race over"),
        )
        frames = _frames_until_race_over(ws)
    assert [
        (frame["stream"], frame["server_id"], frame["payload"]) for frame in frames
    ] == [
        ("servers", created_id, {"change": "created"}),
        ("servers", doomed_id, {"change": "deleted"}),
    ]


# --- mid-stream re-authentication over the real graph (issue #3227) ----------


async def _deactivate(db_url: str, user_id: uuid.UUID) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text('UPDATE "user" SET active = false WHERE id = :id'),
                {"id": user_id},
            )
    finally:
        await engine.dispose()


@pytest.mark.parametrize("stream", ["community", "server"])
async def test_deactivated_user_is_closed_4401_at_the_next_reauthorization(
    _database: str, monkeypatch: pytest.MonkeyPatch, stream: str
) -> None:
    """A real token's socket ends once its account is deactivated.

    Nothing about authentication is faked: the handshake and every
    re-authorization verify the token and load the user row, as REST does per
    request. The socket survives re-authorizations while the account is active
    and closes ``4401`` at the first one after the deactivation commits.
    """

    monkeypatch.setattr(events_module, "_REAUTHZ_INTERVAL_SECONDS", 0.2)
    user = make_user()
    community = await _seed_member(_database, user.id.value)
    server = await _seed_started_server(_database, community, uuid.uuid4())
    bus = InProcessRealTimeEvents()
    app = _app(user, bus, lookup={str(server): community.value})
    del app.dependency_overrides[get_current_user_ws]  # type: ignore[attr-defined]
    client = _client(app)
    token = _build_token_service(
        app.state.settings.auth.token,  # type: ignore[attr-defined]
        SystemClock(),
    ).issue_access_token(user.id)
    url = f"/api/communities/{community.value}/events"
    if stream == "server":
        url = f"/api/communities/{community.value}/servers/{server}/events"

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            url, headers={"Authorization": f"Bearer {token}"}
        ) as ws:
            assert ws.receive_json()["stream"] == "snapshot"
            time.sleep(0.6)  # several re-authorizations of the active account
            _publish(
                client,
                bus,
                server_id=str(server),
                event=RealTimeEvent(
                    stream=EventStream.STATUS, payload={"state": "running"}
                ),
            )
            assert ws.receive_json()["payload"] == {"state": "running"}
            await _deactivate(_database, user.id.value)
            ws.receive_json()
    assert exc.value.code == 4401
