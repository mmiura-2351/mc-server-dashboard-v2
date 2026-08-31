"""Integration tests for the resource-pack repository on PostgreSQL (issue #2612).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). The schema is created and torn down per
test via the real migrations so the adapter runs against the documented shape.

These tests need a **real** statement: the bug is
``fk_srv_rp_assignments_resource_pack_id_resource_packs`` — the schema's only
non-``ON DELETE CASCADE`` foreign key, and not ``DEFERRABLE`` — refusing the
``DELETE FROM resource_packs`` at *statement* end rather than at commit. An
in-memory fake carries no constraints, so it reports success where PostgreSQL
raises (issues #2557, #2549), and the unit-of-work's commit-time translation
never sees the violation at all.

One constraint, two directions (issue #2784): the DELETE is refused *because* an
assignment references the pack, so the pack is in use (409); the assignment
INSERT is refused because the pack it names is gone, which is not-found (404).
The constraint name is the same in both, so only the statement site tells them
apart — and only the real FK raises either. Both directions are pinned here.

The assignment table's *other* FK,
``fk_server_resource_pack_assignments_server_id_server`` (issue #2852), is
``ON DELETE CASCADE``: a server delete sweeps the assignment rather than being
refused, so this one only ever fires on the INSERT, where it means the server is
gone (404). One name, one meaning — the shared map carries it whole.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId as CommunityCommunityId,
)
from mc_server_dashboard_api.community.domain.value_objects import CommunityName
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.servers.adapters.resource_pack_repository import (
    SqlAlchemyResourcePackRepository,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.application.resource_packs import (
    AssignResourcePack,
    DeleteResourcePack,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    ResourcePackInUseError,
    ResourcePackNotFoundError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.resource_pack import (
    ResourcePack,
    ResourcePackAssignment,
    ResourcePackId,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import (
    FakeClock,
    FakeFileStore,
    FakeResourcePackStore,
    FakeVersionValidator,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.timezone.utc)
_UPLOADER = uuid.uuid4()


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


async def _seed_server(engine: AsyncEngine) -> Server:
    community_id = uuid.uuid4()
    community = Community(
        id=CommunityCommunityId(community_id),
        name=CommunityName(f"guild-{community_id}"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    factory = create_session_factory(engine)
    async with CommunityUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.commit()
    return await CreateServer(
        uow=ServersUnitOfWork(factory),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )(
        community_id=CommunityId(community_id),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        config={},
    )


def _pack() -> ResourcePack:
    return ResourcePack(
        id=ResourcePackId.new(),
        filename="pack.zip",
        display_name="Pack",
        description=None,
        sha1_hash="a" * 40,
        sha256_hash="b" * 64,
        size_bytes=128,
        uploaded_by=_UPLOADER,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _assignment(server_id: ServerId, pack_id: ResourcePackId) -> ResourcePackAssignment:
    return ResourcePackAssignment(
        server_id=server_id,
        resource_pack_id=pack_id,
        require_resource_pack=False,
        resource_pack_prompt=None,
        assigned_by=_UPLOADER,
        created_at=_NOW,
        updated_at=_NOW,
    )


async def _seed_pack(engine: AsyncEngine, pack: ResourcePack) -> None:
    async with ServersUnitOfWork(create_session_factory(engine)) as uow:
        await uow.resource_packs.add(pack)
        await uow.commit()


# --- concurrent assign during a pack delete (issue #2612) ---------------------


class _AssignOnListRepository(SqlAlchemyResourcePackRepository):
    """A pack repository that assigns the pack right after reporting it unused.

    Reproduces the production interleave deterministically, with no sleeps: the
    use case's ``list_assignments_for_pack`` pre-check reads no assignment,
    another request's ``AssignResourcePack`` commits on its own connection, and
    only then does ``delete`` run against a row that is now referenced.
    """

    def __init__(
        self, session: AsyncSession, engine: AsyncEngine, server_id: ServerId
    ) -> None:
        super().__init__(session)
        self._engine = engine
        self._server_id = server_id

    async def list_assignments_for_pack(
        self, pack_id: ResourcePackId
    ) -> list[ResourcePackAssignment]:
        assignments = await super().list_assignments_for_pack(pack_id)
        async with ServersUnitOfWork(create_session_factory(self._engine)) as racer:
            await racer.resource_packs.add_assignment(
                _assignment(self._server_id, pack_id)
            )
            await racer.commit()
        return assignments


class _RacingUnitOfWork(ServersUnitOfWork):
    """A servers UnitOfWork wired with :class:`_AssignOnListRepository`."""

    def __init__(self, engine: AsyncEngine, server_id: ServerId) -> None:
        super().__init__(create_session_factory(engine))
        self._engine = engine
        self._server_id = server_id

    async def __aenter__(self) -> _RacingUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        self.resource_packs = _AssignOnListRepository(
            self._session, self._engine, self._server_id
        )
        return self


async def test_delete_of_a_concurrently_assigned_pack_reports_in_use(
    engine: AsyncEngine,
) -> None:
    # fk_srv_rp_assignments_resource_pack_id_resource_packs is NOT DEFERRABLE, so
    # PostgreSQL refuses the DELETE at statement end -- inside delete(), never at
    # the unit of work's commit. The translation therefore has to sit on the
    # execute itself; the map entry alone leaves this a raw IntegrityError (500).
    server = await _seed_server(engine)
    factory = create_session_factory(engine)
    pack = _pack()
    await _seed_pack(engine, pack)

    async with ServersUnitOfWork(factory) as uow:
        assert await uow.resource_packs.list_assignments_for_pack(pack.id) == []
        # A racer assigns the pack to a server after the pre-check saw none.
        async with ServersUnitOfWork(factory) as racer:
            await racer.resource_packs.add_assignment(_assignment(server.id, pack.id))
            await racer.commit()
        with pytest.raises(ResourcePackInUseError):
            await uow.resource_packs.delete(pack.id)


async def test_delete_resource_pack_reports_a_concurrent_assign_as_in_use(
    engine: AsyncEngine,
) -> None:
    # The reachable path: DeleteResourcePack's own in-use pre-check passes, the
    # racer assigns, and the DELETE lands on the live FK. Without the wrap the
    # request dies on a raw IntegrityError (500) despite the map entry.
    server = await _seed_server(engine)
    pack = _pack()
    await _seed_pack(engine, pack)

    use_case = DeleteResourcePack(
        uow=_RacingUnitOfWork(engine, server.id), store=FakeResourcePackStore()
    )
    with pytest.raises(ResourcePackInUseError):
        await use_case(
            resource_pack_id=pack.id,
            caller_id=_UPLOADER,
            is_platform_admin=False,
        )

    # The pack row survives: nothing was deleted behind the typed error.
    async with ServersUnitOfWork(create_session_factory(engine)) as uow:
        assert await uow.resource_packs.get_by_id(pack.id) is not None


# --- concurrent pack delete during an assign (issue #2784) --------------------


class _DeletePackOnWriteFileStore(FakeFileStore):
    """A file store that deletes the pack while the assign writes its properties.

    Reproduces the production interleave deterministically, with no sleeps: the
    use case's first unit of work read the pack and closed, another request's
    ``DeleteResourcePack`` commits on its own connection, and only then does the
    assignment INSERT name a ``resource_packs`` row that is gone.
    """

    def __init__(self, engine: AsyncEngine, pack_id: ResourcePackId) -> None:
        super().__init__()
        self._engine = engine
        self._pack_id = pack_id

    async def write_file(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        content: bytes,
    ) -> None:
        await super().write_file(
            community_id=community_id,
            server_id=server_id,
            rel_path=rel_path,
            content=content,
        )
        async with ServersUnitOfWork(create_session_factory(self._engine)) as racer:
            await racer.resource_packs.delete(self._pack_id)
            await racer.commit()


async def test_assignment_insert_for_a_deleted_pack_reports_not_found(
    engine: AsyncEngine,
) -> None:
    # The same FK, the other direction: the INSERT names a resource_packs row a
    # racer deleted, so the pack is *gone* (404), not in use (409) (issue #2784).
    # add_assignment owns the statement, so the violation surfaces there instead
    # of at the unit of work's commit, whose shared map reads this constraint as
    # the delete direction.
    server = await _seed_server(engine)
    factory = create_session_factory(engine)
    pack = _pack()
    await _seed_pack(engine, pack)

    async with ServersUnitOfWork(factory) as uow:
        assert await uow.resource_packs.get_by_id(pack.id) is not None
        # A racer deletes the pack after the pre-read found it.
        async with ServersUnitOfWork(factory) as racer:
            await racer.resource_packs.delete(pack.id)
            await racer.commit()
        with pytest.raises(ResourcePackNotFoundError):
            await uow.resource_packs.add_assignment(_assignment(server.id, pack.id))


async def test_assign_resource_pack_reports_a_concurrent_delete_as_not_found(
    engine: AsyncEngine,
) -> None:
    # The reachable path: AssignResourcePack's own pre-read finds the pack, the
    # racer deletes it, and the assignment INSERT lands on the live FK. Mapped to
    # 404 by the route -- the very answer the pre-read would have given had the
    # delete landed a moment earlier.
    server = await _seed_server(engine)
    factory = create_session_factory(engine)
    pack = _pack()
    await _seed_pack(engine, pack)

    use_case = AssignResourcePack(
        uow=ServersUnitOfWork(factory),
        file_store=_DeletePackOnWriteFileStore(engine, pack.id),
        clock=FakeClock(_NOW),
    )
    with pytest.raises(ResourcePackNotFoundError):
        await use_case(
            community_id=server.community_id,
            server_id=server.id,
            resource_pack_id=pack.id,
            require_resource_pack=False,
            resource_pack_prompt=None,
            assigned_by=_UPLOADER,
            public_base_url="https://mcsd.example",
        )

    # No assignment row survives the typed error.
    async with ServersUnitOfWork(factory) as uow:
        assert await uow.resource_packs.get_assignment_by_server(server.id) is None


# --- concurrent server delete during an assign (issue #2852) ------------------


async def test_assignment_insert_for_a_deleted_server_reports_not_found(
    engine: AsyncEngine,
) -> None:
    # The assignment table's other parent: the INSERT names a `server` row a racer
    # deleted, so the server is gone -- ServerNotFoundError (404), not a raw
    # IntegrityError (500) (issue #2852). Only the real FK raises this; the
    # in-memory fake carries no constraints. No lifecycle lock reaches this layer,
    # so the repository is where the INSERT direction is reachable at all: the
    # AssignResourcePack / DeleteServer pair above it serialize on one per-server
    # lock, which is what keeps the hole latent rather than live.
    server = await _seed_server(engine)
    factory = create_session_factory(engine)
    pack = _pack()
    await _seed_pack(engine, pack)

    async with ServersUnitOfWork(factory) as uow:
        assert await uow.servers.get_by_id(server.id) is not None
        # A racer deletes the server after the pre-read found it.
        async with ServersUnitOfWork(factory) as racer:
            await racer.servers.delete(server.id)
            await racer.commit()
        with pytest.raises(ServerNotFoundError):
            await uow.resource_packs.add_assignment(_assignment(server.id, pack.id))
