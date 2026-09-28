"""Run the servers repository contracts against PostgreSQL adapters."""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId as CommunityCommunityId,
)
from mc_server_dashboard_api.community.domain.value_objects import CommunityName
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.servers.adapters.unit_of_work import SqlAlchemyUnitOfWork
from mc_server_dashboard_api.servers.domain.backup_repository import BackupRepository
from mc_server_dashboard_api.servers.domain.group_repository import GroupRepository
from mc_server_dashboard_api.servers.domain.plugin_repository import PluginRepository
from mc_server_dashboard_api.servers.domain.repositories import ServerRepository
from mc_server_dashboard_api.servers.domain.resource_pack_repository import (
    ResourcePackRepository,
)
from mc_server_dashboard_api.servers.domain.schedule import ScheduleId
from mc_server_dashboard_api.servers.domain.schedule_repository import (
    ScheduleRepository,
    ScheduleRunRepository,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from tests.contracts.repository import RepositoryHarness, RepositoryTransaction
from tests.contracts.server_repositories import (
    BackupRepositoryContract,
    GroupRepositoryContract,
    PluginRepositoryContract,
    ResourcePackRepositoryContract,
    ScheduleRepositoryContract,
    ScheduleRunRepositoryContract,
    ServerRepositoryContract,
    schedule,
    server,
)
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")
_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.UTC)

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

RepositoryT = TypeVar("RepositoryT")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    integration_engine = create_async_engine(_DB_URL)
    try:
        yield integration_engine
    finally:
        await integration_engine.dispose()
        await downgrade_base(_DB_URL)


def _sql_harness(
    session_factory: async_sessionmaker[AsyncSession],
    repository: Callable[[SqlAlchemyUnitOfWork], RepositoryT],
) -> RepositoryHarness[RepositoryT]:
    @asynccontextmanager
    async def _open() -> AsyncIterator[RepositoryTransaction[RepositoryT]]:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            yield RepositoryTransaction(repository=repository(uow), commit=uow.commit)

    return RepositoryHarness(open=_open)


@pytest.fixture
async def contract_community_id(engine: AsyncEngine) -> CommunityId:
    community_id = CommunityId(uuid.uuid4())
    community = Community(
        id=CommunityCommunityId(community_id.value),
        name=CommunityName(f"contract-{community_id.value}"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with CommunityUnitOfWork(create_session_factory(engine)) as uow:
        await uow.communities.add(community)
        await uow.commit()
    return community_id


@pytest.fixture
async def contract_server_id(
    engine: AsyncEngine, contract_community_id: CommunityId
) -> ServerId:
    item = server(contract_community_id)
    async with SqlAlchemyUnitOfWork(create_session_factory(engine)) as uow:
        await uow.servers.add(item)
        await uow.commit()
    return item.id


@pytest.fixture
async def contract_schedule_id(
    engine: AsyncEngine, contract_server_id: ServerId
) -> ScheduleId:
    item = schedule(contract_server_id)
    async with SqlAlchemyUnitOfWork(create_session_factory(engine)) as uow:
        await uow.schedules.add(item)
        await uow.commit()
    return item.id


@pytest.fixture
def server_repository_harness(
    engine: AsyncEngine,
) -> RepositoryHarness[ServerRepository]:
    return _sql_harness(create_session_factory(engine), lambda uow: uow.servers)


@pytest.fixture
def backup_repository_harness(
    engine: AsyncEngine,
) -> RepositoryHarness[BackupRepository]:
    return _sql_harness(create_session_factory(engine), lambda uow: uow.backups)


@pytest.fixture
def plugin_repository_harness(
    engine: AsyncEngine,
) -> RepositoryHarness[PluginRepository]:
    return _sql_harness(create_session_factory(engine), lambda uow: uow.plugins)


@pytest.fixture
def group_repository_harness(engine: AsyncEngine) -> RepositoryHarness[GroupRepository]:
    return _sql_harness(create_session_factory(engine), lambda uow: uow.groups)


@pytest.fixture
def schedule_repository_harness(
    engine: AsyncEngine,
) -> RepositoryHarness[ScheduleRepository]:
    return _sql_harness(create_session_factory(engine), lambda uow: uow.schedules)


@pytest.fixture
def schedule_run_repository_harness(
    engine: AsyncEngine,
) -> RepositoryHarness[ScheduleRunRepository]:
    return _sql_harness(create_session_factory(engine), lambda uow: uow.schedule_runs)


@pytest.fixture
def resource_pack_repository_harness(
    engine: AsyncEngine,
) -> RepositoryHarness[ResourcePackRepository]:
    return _sql_harness(create_session_factory(engine), lambda uow: uow.resource_packs)


class TestSqlAlchemyServerRepository(ServerRepositoryContract):
    pass


class TestSqlAlchemyBackupRepository(BackupRepositoryContract):
    pass


class TestSqlAlchemyPluginRepository(PluginRepositoryContract):
    pass


class TestSqlAlchemyGroupRepository(GroupRepositoryContract):
    pass


class TestSqlAlchemyScheduleRepository(ScheduleRepositoryContract):
    pass


class TestSqlAlchemyScheduleRunRepository(ScheduleRunRepositoryContract):
    pass


class TestSqlAlchemyResourcePackRepository(ResourcePackRepositoryContract):
    pass
