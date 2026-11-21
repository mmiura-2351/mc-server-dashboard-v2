"""This database's answers to the store owner claim, on PostgreSQL (issue #3886).

The claim asks them of a store that has no marker and already holds objects.

- Whether the communities and resource packs its keys name are all rows here: a
  probe database with a community of its own does not have the live store's.
- For a store of JARs and plugin blobs only, whether this database names any
  stored object: a ``server`` row and a ``resource_packs`` row each answer yes on
  their own; a database with only rows that name nothing in the store (its
  installation, a community) answers no.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId as CommunityCommunityId,
)
from mc_server_dashboard_api.community.domain.value_objects import CommunityName
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.servers.adapters.store_references import (
    SqlAlchemyStoreDatabase,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.resource_pack import (
    ResourcePack,
    ResourcePackId,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import FakeClock, FakeFileStore, FakeVersionValidator

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.UTC)


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


async def _seed_community(engine: AsyncEngine) -> uuid.UUID:
    community = Community(
        id=CommunityCommunityId(uuid.uuid4()),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with CommunityUnitOfWork(create_session_factory(engine)) as uow:
        await uow.communities.add(community)
        await uow.commit()
    return community.id.value


def _database(engine: AsyncEngine) -> SqlAlchemyStoreDatabase:
    return SqlAlchemyStoreDatabase(create_session_factory(engine))


async def _names(engine: AsyncEngine) -> bool:
    return await _database(engine).names_stored_objects()


async def test_a_fresh_database_names_nothing(engine: AsyncEngine) -> None:
    assert await _names(engine) is False


async def test_a_community_alone_names_nothing(engine: AsyncEngine) -> None:
    await _seed_community(engine)

    assert await _names(engine) is False


async def test_a_server_row_names_its_stored_objects(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    await CreateServer(
        uow=ServersUnitOfWork(create_session_factory(engine)),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )(
        community_id=CommunityId(community_id),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="paper",
        config={},
    )

    assert await _names(engine) is True


async def _seed_pack(engine: AsyncEngine) -> uuid.UUID:
    pack = ResourcePack(
        id=ResourcePackId.new(),
        filename="pack.zip",
        display_name="Pack",
        description=None,
        sha1_hash="a" * 40,
        sha256_hash="b" * 64,
        size_bytes=128,
        uploaded_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with ServersUnitOfWork(create_session_factory(engine)) as uow:
        await uow.resource_packs.add(pack)
        await uow.commit()
    return pack.id.value


async def test_a_resource_pack_row_names_its_stored_pack(engine: AsyncEngine) -> None:
    # A pack belongs to no server, so it names a stored object on its own.
    await _seed_pack(engine)

    assert await _names(engine) is True


async def test_it_has_the_communities_and_packs_that_are_rows(
    engine: AsyncEngine,
) -> None:
    community = await _seed_community(engine)
    pack = await _seed_pack(engine)

    has = _database(engine).has
    assert await has(communities=frozenset({community}), resource_packs=frozenset())
    assert await has(communities=frozenset(), resource_packs=frozenset({pack}))
    assert await has(
        communities=frozenset({community}), resource_packs=frozenset({pack})
    )
    assert await has(communities=frozenset(), resource_packs=frozenset())


async def test_it_lacks_a_community_or_pack_another_database_holds(
    engine: AsyncEngine,
) -> None:
    # A probe database with a community and a pack of its own, asked about the
    # live store's: one id it lacks is enough.
    community = await _seed_community(engine)
    pack = await _seed_pack(engine)
    elsewhere = uuid.uuid4()

    has = _database(engine).has
    assert not await has(communities=frozenset({elsewhere}), resource_packs=frozenset())
    assert not await has(
        communities=frozenset({community, elsewhere}), resource_packs=frozenset()
    )
    assert not await has(
        communities=frozenset({community}), resource_packs=frozenset({elsewhere})
    )
    assert not await has(
        communities=frozenset(), resource_packs=frozenset({pack, elsewhere})
    )
