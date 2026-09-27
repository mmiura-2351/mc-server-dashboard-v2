"""Identity repository contracts against the fast in-memory fakes (#3135)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TypeVar

import pytest

from mc_server_dashboard_api.identity.domain.repositories import (
    RefreshTokenRepository,
    UserRepository,
)
from mc_server_dashboard_api.identity.domain.value_objects import UserId
from tests.contracts.identity_repositories import (
    RefreshTokenRepositoryContract,
    RefreshTokenRepositoryHarness,
    UserRepositoryContract,
)
from tests.contracts.repository import RepositoryHarness, RepositoryTransaction
from tests.identity.fakes import FakeRefreshTokenRepository, FakeUserRepository

RepositoryT = TypeVar("RepositoryT")


def _fake_harness(repository: RepositoryT) -> RepositoryHarness[RepositoryT]:
    async def _commit() -> None:
        return None

    @asynccontextmanager
    async def _open() -> AsyncIterator[RepositoryTransaction[RepositoryT]]:
        yield RepositoryTransaction(repository=repository, commit=_commit)

    return RepositoryHarness(open=_open)


@pytest.fixture
def user_repository_harness() -> RepositoryHarness[UserRepository]:
    return _fake_harness(FakeUserRepository())


@pytest.fixture
def refresh_token_repository_harness() -> RefreshTokenRepositoryHarness:
    repository: RefreshTokenRepository = FakeRefreshTokenRepository()
    harness = _fake_harness(repository)
    return RefreshTokenRepositoryHarness(
        open=harness.open,
        user_id=UserId.new(),
        other_user_id=UserId.new(),
    )


class TestFakeUserRepository(UserRepositoryContract):
    pass


class TestFakeRefreshTokenRepository(RefreshTokenRepositoryContract):
    pass
