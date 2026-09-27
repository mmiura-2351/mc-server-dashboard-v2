"""Community repository contracts against PostgreSQL (#3135).

The contract methods are inherited unchanged from ``tests.contracts``.  Only
adapter construction, transaction ownership, and required foreign-key parents
live here so PostgreSQL execution remains in the DB-gated integration lane.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.repositories import (
    CommunityRepository,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    CommunityName,
    UserId,
)
from mc_server_dashboard_api.core.adapters.database import create_session_factory
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


def _community(community_id: CommunityId, name: str) -> Community:
    return Community(
        id=community_id,
        name=CommunityName(name),
        created_at=_NOW,
        updated_at=_NOW,
    )


async def _insert_users(engine: AsyncEngine, *user_ids: UserId) -> None:
    rows = [
        {
            "id": user_id.value,
            "username": f"contract-{user_id.value}",
            "email": f"contract-{user_id.value}@example.com",
        }
        for user_id in user_ids
    ]
    async with engine.begin() as connection:
        await connection.execute(
            text(
                'INSERT INTO "user" '
                "(id, username, email, password_hash, is_platform_admin, "
                "created_at, updated_at) VALUES "
                "(:id, :username, :email, 'hash', false, now(), now())"
            ),
            rows,
        )


async def _insert_communities(
    session_factory: async_sessionmaker[AsyncSession],
    *community_ids: CommunityId,
) -> None:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        for community_id in community_ids:
            await uow.communities.add(
                _community(community_id, f"contract-{community_id.value}")
            )
        await uow.commit()


@pytest.fixture
def community_repository_harness(
    engine: AsyncEngine,
) -> RepositoryHarness[CommunityRepository]:
    return _sql_harness(create_session_factory(engine), lambda uow: uow.communities)


@pytest.fixture
async def membership_repository_harness(
    engine: AsyncEngine,
) -> MembershipRepositoryHarness:
    session_factory = create_session_factory(engine)
    user_id = UserId(uuid.uuid4())
    other_user_id = UserId(uuid.uuid4())
    community_id = CommunityId.new()
    other_community_id = CommunityId.new()
    await _insert_users(engine, user_id, other_user_id)
    await _insert_communities(session_factory, community_id, other_community_id)
    harness = _sql_harness(session_factory, lambda uow: uow.memberships)
    return MembershipRepositoryHarness(
        open=harness.open,
        user_id=user_id,
        other_user_id=other_user_id,
        community_id=community_id,
        other_community_id=other_community_id,
    )


@pytest.fixture
async def role_repository_harness(engine: AsyncEngine) -> RoleRepositoryHarness:
    session_factory = create_session_factory(engine)
    community_id = CommunityId.new()
    other_community_id = CommunityId.new()
    await _insert_communities(session_factory, community_id, other_community_id)
    harness = _sql_harness(session_factory, lambda uow: uow.roles)
    return RoleRepositoryHarness(
        open=harness.open,
        community_id=community_id,
        other_community_id=other_community_id,
    )


@pytest.fixture
async def resource_grant_repository_harness(
    engine: AsyncEngine,
) -> ResourceGrantRepositoryHarness:
    session_factory = create_session_factory(engine)
    user_id = UserId(uuid.uuid4())
    other_user_id = UserId(uuid.uuid4())
    community_id = CommunityId.new()
    other_community_id = CommunityId.new()
    await _insert_users(engine, user_id, other_user_id)
    await _insert_communities(session_factory, community_id, other_community_id)
    harness = _sql_harness(session_factory, lambda uow: uow.resource_grants)
    return ResourceGrantRepositoryHarness(
        open=harness.open,
        user_id=user_id,
        other_user_id=other_user_id,
        community_id=community_id,
        other_community_id=other_community_id,
    )


class TestSqlAlchemyCommunityRepository(CommunityRepositoryContract):
    pass


class TestSqlAlchemyMembershipRepository(MembershipRepositoryContract):
    pass


class TestSqlAlchemyRoleRepository(RoleRepositoryContract):
    pass


class TestSqlAlchemyResourceGrantRepository(ResourceGrantRepositoryContract):
    pass
