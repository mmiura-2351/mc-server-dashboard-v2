"""Community repository contracts against the fast in-memory fakes (#3135)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TypeVar

import pytest

from mc_server_dashboard_api.community.domain.repositories import (
    CommunityRepository,
    MembershipRepository,
    ResourceGrantRepository,
    RoleRepository,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    UserId,
)
from tests.community.fakes import (
    FakeCommunityRepository,
    FakeMembershipRepository,
    FakeResourceGrantRepository,
    FakeRoleRepository,
)
from tests.contracts.community_repositories import (
    CommunityRepositoryContract,
    MembershipRepositoryContract,
    MembershipRepositoryHarness,
    ResourceGrantRepositoryContract,
    ResourceGrantRepositoryHarness,
    RoleRepositoryContract,
    RoleRepositoryHarness,
)
from tests.contracts.repository import RepositoryHarness, RepositoryTransaction

RepositoryT = TypeVar("RepositoryT")


def _fake_harness(repository: RepositoryT) -> RepositoryHarness[RepositoryT]:
    async def _commit() -> None:
        return None

    @asynccontextmanager
    async def _open() -> AsyncIterator[RepositoryTransaction[RepositoryT]]:
        yield RepositoryTransaction(repository=repository, commit=_commit)

    return RepositoryHarness(open=_open)


@pytest.fixture
def community_repository_harness() -> RepositoryHarness[CommunityRepository]:
    return _fake_harness(FakeCommunityRepository())


@pytest.fixture
def membership_repository_harness() -> MembershipRepositoryHarness:
    repository: MembershipRepository = FakeMembershipRepository()
    harness = _fake_harness(repository)
    return MembershipRepositoryHarness(
        open=harness.open,
        user_id=UserId(uuid.uuid4()),
        other_user_id=UserId(uuid.uuid4()),
        community_id=CommunityId.new(),
        other_community_id=CommunityId.new(),
    )


@pytest.fixture
def role_repository_harness() -> RoleRepositoryHarness:
    repository: RoleRepository = FakeRoleRepository()
    harness = _fake_harness(repository)
    return RoleRepositoryHarness(
        open=harness.open,
        community_id=CommunityId.new(),
        other_community_id=CommunityId.new(),
    )


@pytest.fixture
def resource_grant_repository_harness() -> ResourceGrantRepositoryHarness:
    repository: ResourceGrantRepository = FakeResourceGrantRepository()
    harness = _fake_harness(repository)
    return ResourceGrantRepositoryHarness(
        open=harness.open,
        user_id=UserId(uuid.uuid4()),
        other_user_id=UserId(uuid.uuid4()),
        community_id=CommunityId.new(),
        other_community_id=CommunityId.new(),
    )


class TestFakeCommunityRepository(CommunityRepositoryContract):
    pass


class TestFakeMembershipRepository(MembershipRepositoryContract):
    pass


class TestFakeRoleRepository(RoleRepositoryContract):
    pass


class TestFakeResourceGrantRepository(ResourceGrantRepositoryContract):
    pass
