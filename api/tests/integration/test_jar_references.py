"""Integration test for the JAR-pool GC live-reference adapter on PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Verifies that
:class:`ServerJarReferences` reads the resolved-JAR content key off real server
rows' ``config`` blob and returns the distinct set the GC diffs the pool against
(issue #293). The rows matter, so this is a DB test, not a unit test.
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
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    JAR_KEY_CONFIG_FIELD,
    CommunityId,
)
from mc_server_dashboard_api.versions.adapters.server_jar_references import (
    ServerJarReferences,
)
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import FakeClock, FakeFileStore, FakeVersionValidator

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 5, 12, 0, tzinfo=dt.timezone.utc)
_SHA_A = "a" * 64
_SHA_B = "b" * 64


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


async def _create_server(
    engine: AsyncEngine, community_id: uuid.UUID, name: str, *, jar: str | None
) -> None:
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
        name=name,
        mc_edition="java",
        mc_version="1.21.1",
        server_type="paper",
        execution_backend="container",
        config={},
    )
    if jar is not None:
        async with ServersUnitOfWork(factory) as uow:
            loaded = await uow.servers.get_by_id(server.id)
            assert loaded is not None
            loaded.config = {**loaded.config, JAR_KEY_CONFIG_FIELD: jar}
            await uow.servers.update(loaded)
            await uow.commit()


async def test_live_returns_distinct_resolved_jar_keys(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    # Two servers reference SHA_A, one references SHA_B, one has no resolved JAR.
    await _create_server(engine, community_id, "a1", jar=_SHA_A)
    await _create_server(engine, community_id, "a2", jar=_SHA_A)
    await _create_server(engine, community_id, "b1", jar=_SHA_B)
    await _create_server(engine, community_id, "none", jar=None)

    refs = ServerJarReferences(uow=ServersUnitOfWork(create_session_factory(engine)))
    assert await refs.live() == {_SHA_A, _SHA_B}


async def test_live_is_empty_when_no_server_has_a_resolved_jar(
    engine: AsyncEngine,
) -> None:
    community_id = await _seed_community(engine)
    await _create_server(engine, community_id, "plain", jar=None)

    refs = ServerJarReferences(uow=ServersUnitOfWork(create_session_factory(engine)))
    assert await refs.live() == set()
