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
    UpdateServer,
)
from mc_server_dashboard_api.servers.domain.errors import (
    PortAlreadyTakenError,
    ServerNameAlreadyExistsError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import (
    FakeBackupArchiveStore,
    FakeClock,
    FakeFileStore,
    FakeVersionValidator,
)

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
    create = CreateServer(
        uow=ServersUnitOfWork(factory),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )
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
    create = CreateServer(
        uow=ServersUnitOfWork(factory),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )
    await create(
        community_id=CommunityId(community_id),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={},
    )
    with pytest.raises(ServerNameAlreadyExistsError):
        await create(
            community_id=CommunityId(community_id),
            name="survival",
            mc_edition="java",
            mc_version="1.21.1",
            server_type="vanilla",
            execution_backend="container",
            config={},
        )


async def test_delete_sweeps_resource_grants(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    user_id = uuid.uuid4()
    await _insert_user(engine, user_id, "alice")
    factory = create_session_factory(engine)

    # Create the server first so we have its id for the grant's resource_id.
    create = CreateServer(
        uow=ServersUnitOfWork(factory),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )
    server = await create(
        community_id=CommunityId(community_id),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
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
    await DeleteServer(
        uow=ServersUnitOfWork(factory),
        backup_store=FakeBackupArchiveStore(),
    )(community_id=CommunityId(community_id), server_id=server.id)

    async with ServersUnitOfWork(factory) as uow:
        assert await uow.servers.get_by_id(server.id) is None
    async with CommunityUnitOfWork(factory) as uow:
        assert await uow.resource_grants.get_by_id(grant.id) is None


async def test_server_id_isolation_across_communities(engine: AsyncEngine) -> None:
    community_a = await _seed_community(engine)
    factory = create_session_factory(engine)
    create = CreateServer(
        uow=ServersUnitOfWork(factory),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )
    server = await create(
        community_id=CommunityId(community_a),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={},
    )
    # Reading the A server scoped to a different (random) community id misses, so
    # a server id from community A cannot be reached through another community.
    with pytest.raises(ServerNotFoundError):
        await ReadServer(uow=ServersUnitOfWork(factory))(
            community_id=CommunityId(uuid.uuid4()), server_id=server.id
        )


async def _insert_legacy_server(
    engine: AsyncEngine, community_id: uuid.UUID, name: str
) -> uuid.UUID:
    """Insert a row with ``game_port = NULL`` (a pre-#243 legacy/imported row)."""

    server_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO server "
                "(id, community_id, name, mc_edition, mc_version, server_type, "
                "execution_backend, config, game_port, desired_state, "
                "observed_state, created_at, updated_at) VALUES "
                "(:id, :community_id, :name, 'java', '1.21.1', 'vanilla', "
                "'host_process', '{}', NULL, 'stopped', 'stopped', now(), now())"
            ),
            {"id": server_id, "community_id": community_id, "name": name},
        )
    return server_id


async def test_list_ids_missing_game_port_finds_only_legacy_rows(
    engine: AsyncEngine,
) -> None:
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    create = CreateServer(
        uow=ServersUnitOfWork(factory),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )
    # A normally-created server carries an auto-assigned port; a legacy row does
    # not (issue #310).
    tracked = await create(
        community_id=CommunityId(community_id),
        name="tracked",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={},
    )
    legacy_id = await _insert_legacy_server(engine, community_id, "legacy")

    async with ServersUnitOfWork(factory) as uow:
        missing = await uow.servers.list_ids_missing_game_port()
        taken = await uow.servers.list_game_ports()

    # Only the legacy row is reported missing; the tracked row's port is in the
    # taken set, and the legacy NULL port is excluded from it.
    assert [s.value for s in missing] == [legacy_id]
    assert tracked.game_port in taken


async def _grant_all(_code: str) -> bool:
    """A permissive ``authorize`` for tests that exercise non-authz behavior."""

    return True


def _updater(factory: object) -> UpdateServer:
    return UpdateServer(
        uow=ServersUnitOfWork(factory),  # type: ignore[arg-type]
        clock=FakeClock(_NOW),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )


async def test_update_game_port_persists_to_row(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    create = CreateServer(
        uow=ServersUnitOfWork(factory),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )
    server = await create(
        community_id=CommunityId(community_id),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={},
    )

    await _updater(factory)(
        community_id=CommunityId(community_id),
        server_id=server.id,
        game_port=25570,
        authorize=_grant_all,
    )

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server.id)
        taken = await uow.servers.list_game_ports()
    assert loaded is not None
    assert loaded.game_port == 25570
    # The new port is in the taken set; the old port was released.
    assert taken == {25570}


async def test_update_game_port_rejects_taken_against_real_db(
    engine: AsyncEngine,
) -> None:
    # An at-rest re-port to a port another server already holds in the DB is a
    # PortAlreadyTakenError: the taken-set pre-read (against the real DB) catches
    # the conflict. The deployment-wide UNIQUE(game_port) is the ultimate backstop
    # for a genuine race that slips past the pre-read (#261).
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    create = CreateServer(
        uow=ServersUnitOfWork(factory),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )
    server = await create(
        community_id=CommunityId(community_id),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={},
    )
    # Another server already holds 25570 in the DB; the pre-read catches it.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO server "
                "(id, community_id, name, mc_edition, mc_version, server_type, "
                "execution_backend, config, game_port, desired_state, "
                "observed_state, created_at, updated_at) VALUES "
                "(:id, :community_id, 'taker', 'java', '1.21.1', 'vanilla', "
                "'host_process', '{}', 25570, 'stopped', 'stopped', now(), now())"
            ),
            {"id": uuid.uuid4(), "community_id": community_id},
        )

    with pytest.raises(PortAlreadyTakenError):
        await _updater(factory)(
            community_id=CommunityId(community_id),
            server_id=server.id,
            game_port=25570,
            authorize=_grant_all,
        )

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server.id)
    assert loaded is not None
    assert loaded.game_port == server.game_port
