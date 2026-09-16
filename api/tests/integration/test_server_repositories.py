"""Integration tests for the servers repository + UnitOfWork on PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). The schema is created and torn down per
test via the real migrations so the adapters run against the documented shape
(DATABASE.md Section 7, 10). A community (and, for the sweep test, a user +
membership + resource grant) are seeded through the community adapters; the
server-delete grant sweep is exercised end to end via :class:`DeleteServer`.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import uuid
import zipfile
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
from mc_server_dashboard_api.servers.adapters.repositories import (
    SqlAlchemyServerRepository,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.export_import import (
    EXPORT_FORMAT_VERSION,
    EXPORT_METADATA_FILENAME,
    ImportServer,
)
from mc_server_dashboard_api.servers.application.manage_server import (
    CreateServer,
    DeleteServer,
    ReadServer,
    UpdateServer,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    CommunityNotFoundError,
    PortAlreadyTakenError,
    ServerNameAlreadyExistsError,
    ServerNotFoundError,
    SlugAlreadyTakenError,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
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
        config={"motd": "hi", "max-players": 20},
    )

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(created.id)
        listed = await uow.servers.list_for_community(CommunityId(community_id))

    assert loaded is not None
    assert loaded.config == {"motd": "hi", "max-players": 20}
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
        config={},
    )
    with pytest.raises(ServerNameAlreadyExistsError):
        await create(
            community_id=CommunityId(community_id),
            name="survival",
            mc_edition="java",
            mc_version="1.21.1",
            server_type="vanilla",
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
                "config, game_port, slug, desired_state, "
                "observed_state, created_at, updated_at) VALUES "
                "(:id, :community_id, :name, 'java', '1.21.1', 'vanilla', "
                "'{}', NULL, :slug, 'stopped', 'stopped', now(), now())"
            ),
            {
                "id": server_id,
                "community_id": community_id,
                "name": name,
                "slug": f"legacy-{str(server_id)[:8]}-00",
            },
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


def _updater(
    factory: object, *, file_store: FakeFileStore | None = None
) -> UpdateServer:
    return UpdateServer(
        uow=ServersUnitOfWork(factory),  # type: ignore[arg-type]
        clock=FakeClock(_NOW),
        file_store=file_store or FakeFileStore(),
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
        config={},
    )

    # The port rewrite preserves the file's other keys, so an absent
    # server.properties is refused before the commit (#2623). The subject here
    # is the row, so seed the file the update rewrites.
    file_store = FakeFileStore()
    file_store.files["server.properties"] = f"server-port={server.game_port}\n".encode()

    await _updater(factory, file_store=file_store)(
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
        config={},
    )
    # Another server already holds 25570 in the DB; the pre-read catches it.
    taker_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO server "
                "(id, community_id, name, mc_edition, mc_version, server_type, "
                "config, game_port, slug, desired_state, "
                "observed_state, created_at, updated_at) VALUES "
                "(:id, :community_id, 'taker', 'java', '1.21.1', 'vanilla', "
                "'{}', 25570, :slug, "
                "'stopped', 'stopped', now(), now())"
            ),
            {
                "id": taker_id,
                "community_id": community_id,
                "slug": f"taker-{str(taker_id)[:8]}-00",
            },
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


# --- bedrock_port (issue #1541) ----------------------------------------------


def _creator(factory: object) -> CreateServer:
    return CreateServer(
        uow=ServersUnitOfWork(factory),  # type: ignore[arg-type]
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )


async def test_bedrock_port_persists_lists_and_releases(engine: AsyncEngine) -> None:
    # The Geyser-detection write path: stage bedrock_port through the repository
    # update, read it back, see it in the deployment-wide taken set, and release
    # it (NULL) the way a Geyser uninstall does.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    server = await _creator(factory)(
        community_id=CommunityId(community_id),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        config={},
    )

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server.id)
        assert loaded is not None
        loaded.bedrock_port = 19132
        await uow.servers.update(loaded)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        reloaded = await uow.servers.get_by_id(server.id)
        taken = await uow.servers.list_bedrock_ports()
    assert reloaded is not None
    assert reloaded.bedrock_port == 19132
    assert taken == {19132}

    async with ServersUnitOfWork(factory) as uow:
        reloaded.bedrock_port = None
        await uow.servers.update(reloaded)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        assert await uow.servers.list_bedrock_ports() == set()


async def test_bedrock_port_unique_backstop_and_delete_release(
    engine: AsyncEngine,
) -> None:
    # UNIQUE(bedrock_port) rejects a duplicate allocation (the concurrent-racer
    # backstop). The violating write is the repository UPDATE executed inside
    # the transaction, where the adapter translates the driver IntegrityError
    # into the typed PortAlreadyTakenError (issue #1541) so the install
    # endpoints return a retryable 409, not a raw 500. Deleting the holder's
    # row releases the port for reuse.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    creator = _creator(factory)
    first = await creator(
        community_id=CommunityId(community_id),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        config={},
    )
    second = await creator(
        community_id=CommunityId(community_id),
        name="creative",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        config={},
    )

    async def _set_port(server_id: object, port: int) -> None:
        async with ServersUnitOfWork(factory) as uow:
            loaded = await uow.servers.get_by_id(server_id)  # type: ignore[arg-type]
            assert loaded is not None
            loaded.bedrock_port = port
            await uow.servers.update(loaded)
            await uow.commit()

    await _set_port(first.id, 19132)
    with pytest.raises(PortAlreadyTakenError):
        await _set_port(second.id, 19132)

    async with ServersUnitOfWork(factory) as uow:
        await uow.servers.delete(first.id)
        await uow.commit()

    # The row delete released the port: the same value is assignable again.
    await _set_port(second.id, 19132)
    async with ServersUnitOfWork(factory) as uow:
        assert await uow.servers.list_bedrock_ports() == {19132}


async def test_backup_retention_round_trip_and_clear(engine: AsyncEngine) -> None:
    # The nullable backup_retention jsonb column (issue #1841, migration 0032):
    # NULL by default, set via the narrow update_backup_retention write, read
    # back on the entity, and clearable back to NULL.
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
        server_type="vanilla",
        config={},
    )
    assert created.backup_retention is None

    async with ServersUnitOfWork(factory) as uow:
        await uow.servers.update_backup_retention(created.id, {"keep_last": 3})
        await uow.commit()
    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(created.id)
    assert loaded is not None
    assert loaded.backup_retention == {"keep_last": 3}

    async with ServersUnitOfWork(factory) as uow:
        await uow.servers.update_backup_retention(
            created.id, {"daily": 7, "weekly": 4, "monthly": 6}
        )
        await uow.commit()
    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(created.id)
    assert loaded is not None
    assert loaded.backup_retention == {"daily": 7, "weekly": 4, "monthly": 6}

    async with ServersUnitOfWork(factory) as uow:
        await uow.servers.update_backup_retention(created.id, None)
        await uow.commit()
    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(created.id)
    assert loaded is not None
    assert loaded.backup_retention is None


# --- the create INSERT's parent community, deleted mid-create (issue #2940) ---


def _server_entity(community_id: uuid.UUID) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=CommunityId(community_id),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


async def test_commit_after_concurrent_community_delete_reports_not_found(
    engine: AsyncEngine,
) -> None:
    # fk_server_community_id_community is ON DELETE CASCADE, so it is violable
    # only by a racer deleting the community between the request's read of it and
    # this INSERT. The row is staged with ``session.add``, so -- unlike the group
    # create's explicit flush (#2924) -- the statement that emits it is the unit
    # of work's ``commit``, and that is the wrap the translation has to be reached
    # through. Live FK, so this pins the real constraint name and the real site
    # rather than a fake's opinion of either.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)

    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM community WHERE id = :id"), {"id": community_id}
        )

    async with ServersUnitOfWork(factory) as uow:
        await uow.servers.add(_server_entity(community_id))
        with pytest.raises(CommunityNotFoundError):
            await uow.commit()


async def test_create_server_reports_a_concurrent_community_delete_as_not_found(
    engine: AsyncEngine,
) -> None:
    # The reachable path. CreateServer's pre-reads inside the transaction are the
    # taken-port and taken-slug sets, both deployment-wide rather than
    # community-scoped, so neither notices the missing community and nothing
    # short-circuits the INSERT: the use case really does reach the commit and
    # depends on the translation for its typed error. The community pre-read that
    # would have caught this is the authorization gate, one layer up.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    create = CreateServer(
        uow=ServersUnitOfWork(factory),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )

    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM community WHERE id = :id"), {"id": community_id}
        )

    with pytest.raises(CommunityNotFoundError):
        await create(
            community_id=CommunityId(community_id),
            name="survival",
            mc_edition="java",
            mc_version="1.21.1",
            server_type="vanilla",
            config={},
        )


# --- the import's auto-assigned port / slug, taken mid-create (issue #3022) ---
#
# ImportServer composes CreateServer with NEITHER an explicit game port nor an
# explicit slug, so both are auto-assigned: chosen from a deployment-wide
# taken-set read inside the transaction and committed later. A racer that commits
# the same value in that window slips past the pre-read, so uq_server_game_port /
# uq_server_slug fire at the unit of work's commit -- the same site the community
# FK of #2940 fires at, for the same reason (nothing between the ``add`` and the
# commit flushes). Auto-assignment is what makes the window exist, not what closes
# it. These pin that the race is real *through import* and that the seam hands the
# route a typed error rather than a raw IntegrityError; the route arms that turn
# each into its status are pinned in tests/servers/test_export_import_endpoints.py.


async def _insert_racer(
    engine: AsyncEngine, community_id: uuid.UUID, *, game_port: int, slug: str
) -> None:
    """Commit a rival server row holding *game_port* / *slug* on its own connection."""

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO server "
                "(id, community_id, name, mc_edition, mc_version, server_type, "
                "config, game_port, slug, desired_state, "
                "observed_state, created_at, updated_at) VALUES "
                "(:id, :community_id, 'racer', 'java', '1.21.1', 'vanilla', "
                "'{}', :game_port, :slug, "
                "'stopped', 'stopped', now(), now())"
            ),
            {
                "id": uuid.uuid4(),
                "community_id": community_id,
                "game_port": game_port,
                "slug": slug,
            },
        )


def _export_archive() -> bytes:
    """A minimal valid export zip: the metadata descriptor and nothing else."""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr(
            EXPORT_METADATA_FILENAME,
            json.dumps(
                {
                    "format": EXPORT_FORMAT_VERSION,
                    "name": "exported",
                    "mc_edition": "java",
                    "mc_version": "1.21.1",
                    "server_type": "vanilla",
                }
            ),
        )
    return buf.getvalue()


def _importer(factory: object) -> ImportServer:
    return ImportServer(create_server=_creator(factory), file_store=FakeFileStore())


async def test_import_racing_a_taken_game_port_reports_port_taken(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The racer commits between CreateServer's ``list_game_ports()`` pre-read and
    # the commit that emits our INSERT, which is exactly the window the constraint
    # exists for; hooking the repository's ``add`` is how a single-threaded test
    # lands inside it, and it also gives the racer the very port this create just
    # picked. The racer's slug carries a hyphen, which generate_slug's
    # ``[a-z0-9]{6}`` alphabet cannot produce, so only the port can collide.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    original_add = SqlAlchemyServerRepository.add

    async def racing_add(self: SqlAlchemyServerRepository, server: Server) -> None:
        assert server.game_port is not None
        await _insert_racer(
            engine, community_id, game_port=server.game_port, slug="racer-slug"
        )
        await original_add(self, server)

    monkeypatch.setattr(SqlAlchemyServerRepository, "add", racing_add)

    with pytest.raises(PortAlreadyTakenError):
        await _importer(factory)(
            community_id=CommunityId(community_id),
            name="imported",
            content=_export_archive(),
        )


async def test_import_racing_a_taken_slug_reports_slug_taken(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same window one constraint over, with the racer taking the slug this
    # create just generated. Its port is outside the creator's 25565-25664 range,
    # so only the slug can collide.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    original_add = SqlAlchemyServerRepository.add

    async def racing_add(self: SqlAlchemyServerRepository, server: Server) -> None:
        await _insert_racer(engine, community_id, game_port=30000, slug=server.slug)
        await original_add(self, server)

    monkeypatch.setattr(SqlAlchemyServerRepository, "add", racing_add)

    with pytest.raises(SlugAlreadyTakenError):
        await _importer(factory)(
            community_id=CommunityId(community_id),
            name="imported",
            content=_export_archive(),
        )
