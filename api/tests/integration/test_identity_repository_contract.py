"""Identity repository contracts against PostgreSQL (#3135)."""

from __future__ import annotations

import datetime as dt
import os
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

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.identity.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.repositories import UserRepository
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    UserId,
    Username,
)
from tests.contracts.identity_repositories import (
    RefreshTokenRepositoryContract,
    RefreshTokenRepositoryHarness,
    UserRepositoryContract,
)
from tests.contracts.repository import RepositoryHarness, RepositoryTransaction
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


def _user(user_id: UserId, username: str) -> User:
    return User(
        id=user_id,
        username=Username(username),
        email=EmailAddress(f"{username}@example.com"),
        password_hash="hash",
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.fixture
def user_repository_harness(
    engine: AsyncEngine,
) -> RepositoryHarness[UserRepository]:
    return _sql_harness(create_session_factory(engine), lambda uow: uow.users)


@pytest.fixture
async def refresh_token_repository_harness(
    engine: AsyncEngine,
) -> RefreshTokenRepositoryHarness:
    session_factory = create_session_factory(engine)
    user_id = UserId.new()
    other_user_id = UserId.new()
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.users.add(_user(user_id, "contract-alice"))
        await uow.users.add(_user(other_user_id, "contract-bob"))
        await uow.commit()
    harness = _sql_harness(session_factory, lambda uow: uow.refresh_tokens)
    return RefreshTokenRepositoryHarness(
        open=harness.open,
        user_id=user_id,
        other_user_id=other_user_id,
    )


class TestSqlAlchemyUserRepository(UserRepositoryContract):
    pass


class TestSqlAlchemyRefreshTokenRepository(RefreshTokenRepositoryContract):
    pass
