"""Integration tests for the servers repository + UnitOfWork on PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). The schema is created and torn down per
test via the real 0001-0005 migrations so the adapters run against the documented
shape (DATABASE.md Section 7, 10). A community (and, for the sweep test, a user +
membership + resource grant) are seeded through the community adapters; the
server-delete grant sweep is exercised end to end via :class:`DeleteServer`.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import (
    Community,
    Membership,
    ResourceGrant,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId as CommunityCommunityId,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityName,
    MembershipId,
    Permission,
    ResourceGrantId,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    UserId as CommunityUserId,
)
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.manage_server import (
    CreateServer,
    DeleteServer,
    ReadServer,
)
from mc_server_dashboard_api.servers.domain.errors import (
    ServerNameAlreadyExistsError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import FakeClock

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


async def _insert_user(engine: AsyncEngine, user_id: uuid.UUID, username: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                'INSERT INTO "user" '
                "(id, username, email, password_hash, is_platform_admin, "
                "created_at, updated_at) VALUES "
                "(:id, :username, :email, 'h', false, now(), now())"
            ),
            {"id": user_id, "username": username, "email": f"{username}@e.com"},
        )


async def _seed_community(engine: AsyncEngine) -> uuid.UUID:
    community = Community(
        id=CommunityCommunityId(uuid.uuid4()),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    factory = create_session_factory(engine)
    async with CommunityUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.commit()
    return community.id.value


async def test_create_then_read_back(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    create = CreateServer(uow=ServersUnitOfWork(factory), clock=FakeClock(_NOW))
    created = await create(
        community_id=CommunityId(community_id),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="paper",
        execution_backend="container",
        config={"motd": "hi", "max-players": 20},
    )

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(created.id)
        listed = await uow.servers.list_for_community(CommunityId(community_id))

    assert loaded is not None
    assert loaded.config == {"motd": "hi", "max-players": 20}
    assert loaded.execution_backend.value == "container"
    assert loaded.observed_at is None
    assert loaded.assigned_worker_id is None
    assert [s.id for s in listed] == [created.id]


async def test_duplicate_name_in_community_conflicts(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    create = CreateServer(uow=ServersUnitOfWork(factory), clock=FakeClock(_NOW))
    await create(
        community_id=CommunityId(community_id),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="host_process",
        config={},
    )
    with pytest.raises(ServerNameAlreadyExistsError):
        await create(
            community_id=CommunityId(community_id),
            name="survival",
            mc_edition="java",
            mc_version="1.21.1",
            server_type="vanilla",
            execution_backend="host_process",
            config={},
        )


async def test_delete_sweeps_resource_grants(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    user_id = uuid.uuid4()
    await _insert_user(engine, user_id, "alice")
    factory = create_session_factory(engine)

    # Create the server first so we have its id for the grant's resource_id.
    create = CreateServer(uow=ServersUnitOfWork(factory), clock=FakeClock(_NOW))
    server = await create(
        community_id=CommunityId(community_id),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="host_process",
        config={},
    )

    # Seed a membership + a resource grant on that server (community context).
    com = CommunityCommunityId(community_id)
    user = CommunityUserId(user_id)
    grant = ResourceGrant(
        id=ResourceGrantId.new(),
        user_id=user,
        community_id=com,
        resource_type="server",
        resource_id=server.id.value,
        permissions={Permission("server:read")},
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with CommunityUnitOfWork(factory) as uow:
        await uow.memberships.add(
            Membership(
                id=MembershipId.new(),
                user_id=user,
                community_id=com,
                created_at=_NOW,
            )
        )
        await uow.resource_grants.add(grant)
        await uow.commit()

    # Delete the server: the grant on it must be swept in the same transaction.
    await DeleteServer(uow=ServersUnitOfWork(factory))(
        community_id=CommunityId(community_id), server_id=server.id
    )

    async with ServersUnitOfWork(factory) as uow:
        assert await uow.servers.get_by_id(server.id) is None
    async with CommunityUnitOfWork(factory) as uow:
        assert await uow.resource_grants.get_by_id(grant.id) is None


async def test_server_id_isolation_across_communities(engine: AsyncEngine) -> None:
    community_a = await _seed_community(engine)
    factory = create_session_factory(engine)
    create = CreateServer(uow=ServersUnitOfWork(factory), clock=FakeClock(_NOW))
    server = await create(
        community_id=CommunityId(community_a),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="host_process",
        config={},
    )
    # Reading the A server scoped to a different (random) community id misses, so
    # a server id from community A cannot be reached through another community.
    with pytest.raises(ServerNotFoundError):
        await ReadServer(uow=ServersUnitOfWork(factory))(
            community_id=CommunityId(uuid.uuid4()), server_id=server.id
        )
