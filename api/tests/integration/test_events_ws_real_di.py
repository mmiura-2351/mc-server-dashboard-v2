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

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient
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
    get_real_time_events,
    get_server_community_lookup,
)
from mc_server_dashboard_api.fleet.adapters.real_time_events import (
    InProcessRealTimeEvents,
)
from mc_server_dashboard_api.fleet.domain.real_time_events import (
    EventStream,
    RealTimeEvent,
)
from mc_server_dashboard_api.identity.domain.entities import User
from tests.identity.fakes import make_user
from tests.integration.migrate import downgrade_base, upgrade_head

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
    app.dependency_overrides[get_current_user_ws] = lambda: user
    app.dependency_overrides[get_real_time_events] = lambda: bus
    app.dependency_overrides[get_server_community_lookup] = lambda: _FakeLookup(
        lookup or {}
    )
    return app


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


async def test_community_events_real_graph_accepts_and_delivers(
    _database: str,
) -> None:
    user = make_user()
    community = await _seed_member(_database, user.id.value)
    bus = InProcessRealTimeEvents()
    server = uuid.uuid4()
    client = next(_client(_app(user, bus, lookup={str(server): community.value})))
    url = f"/api/communities/{community.value}/events"
    with client.websocket_connect(url) as ws:
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(
                stream=EventStream.STATUS, payload={"state": "running"}
            ),
        )
        frame = ws.receive_json()
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
    client = next(_client(_app(user, bus)))
    url = f"/api/communities/{community.value}/servers/{server}/events"
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url):
            pass
    assert exc.value.code == _CLOSE_NOT_FOUND
