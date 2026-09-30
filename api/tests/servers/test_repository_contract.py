"""Run the servers repository contracts against the fast in-memory fakes."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TypeVar

import pytest

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
)
from tests.servers.fakes import (
    FakeBackupRepository,
    FakeGroupRepository,
    FakePluginRepository,
    FakeResourcePackRepository,
    FakeScheduleRepository,
    FakeScheduleRunRepository,
    FakeServerRepository,
)

RepositoryT = TypeVar("RepositoryT")


def _fake_harness(repository: RepositoryT) -> RepositoryHarness[RepositoryT]:
    async def _commit() -> None:
        return None

    @asynccontextmanager
    async def _open() -> AsyncIterator[RepositoryTransaction[RepositoryT]]:
        yield RepositoryTransaction(repository=repository, commit=_commit)

    return RepositoryHarness(open=_open)


@pytest.fixture
def contract_community_id() -> CommunityId:
    return CommunityId(uuid.uuid4())


@pytest.fixture
def contract_server_id() -> ServerId:
    return ServerId(uuid.uuid4())


@pytest.fixture
def contract_schedule_id() -> ScheduleId:
    return ScheduleId.new()


@pytest.fixture
def server_repository_harness() -> RepositoryHarness[ServerRepository]:
    return _fake_harness(FakeServerRepository())


@pytest.fixture
def backup_repository_harness() -> RepositoryHarness[BackupRepository]:
    return _fake_harness(FakeBackupRepository())


@pytest.fixture
def plugin_repository_harness() -> RepositoryHarness[PluginRepository]:
    return _fake_harness(FakePluginRepository())


@pytest.fixture
def group_repository_harness() -> RepositoryHarness[GroupRepository]:
    return _fake_harness(FakeGroupRepository())


@pytest.fixture
def schedule_repository_harness() -> RepositoryHarness[ScheduleRepository]:
    return _fake_harness(FakeScheduleRepository())


@pytest.fixture
def schedule_run_repository_harness() -> RepositoryHarness[ScheduleRunRepository]:
    return _fake_harness(FakeScheduleRunRepository())


@pytest.fixture
def resource_pack_repository_harness() -> RepositoryHarness[ResourcePackRepository]:
    return _fake_harness(FakeResourcePackRepository())


class TestFakeServerRepository(ServerRepositoryContract):
    pass


class TestFakeBackupRepository(BackupRepositoryContract):
    pass


class TestFakePluginRepository(PluginRepositoryContract):
    pass


class TestFakeGroupRepository(GroupRepositoryContract):
    pass


class TestFakeScheduleRepository(ScheduleRepositoryContract):
    pass


class TestFakeScheduleRunRepository(ScheduleRunRepositoryContract):
    pass


class TestFakeResourcePackRepository(ResourcePackRepositoryContract):
    pass
