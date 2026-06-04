"""Integration tests for the identity repositories + UnitOfWork on PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). The schema is created and torn down per
test via the real 0002 migration so the adapters run against the documented
shape.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.identity.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.identity.domain.entities import RefreshToken, User
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    RefreshTokenId,
    UserId,
    Username,
)
from tests.integration.migrate import downgrade_base, upgrade_head

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


def _user(username: str = "alice", email: str = "alice@example.com") -> User:
    return User(
        id=UserId.new(),
        username=Username(username),
        email=EmailAddress(email),
        password_hash="hash",
        created_at=_NOW,
        updated_at=_NOW,
    )


async def test_add_user_and_read_back_by_id(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    user = _user()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.users.add(user)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        loaded = await uow.users.get_by_id(user.id)

    assert loaded is not None
    assert loaded.id == user.id
    assert loaded.username == user.username
    assert loaded.email == user.email
    assert loaded.is_platform_admin is False


async def test_lookup_user_by_username_is_case_insensitive(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    user = _user(username="Alice")

    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.users.add(user)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        loaded = await uow.users.get_by_username(Username("alice"))

    assert loaded is not None
    assert loaded.id == user.id


async def test_lookup_user_by_email(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    user = _user()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.users.add(user)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        loaded = await uow.users.get_by_email(EmailAddress("alice@example.com"))

    assert loaded is not None
    assert loaded.id == user.id


async def test_missing_user_returns_none(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert await uow.users.get_by_id(UserId.new()) is None


async def test_rollback_when_block_not_committed(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    user = _user()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.users.add(user)
        # No commit: leaving the block must roll back.

    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert await uow.users.get_by_id(user.id) is None


async def test_add_refresh_token_and_read_back(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    user = _user()
    token = RefreshToken(
        id=RefreshTokenId.new(),
        user_id=user.id,
        token_hash="hashed-token",
        issued_at=_NOW,
        expires_at=_NOW + dt.timedelta(days=30),
    )

    # The user is created first (registration), then a session token is issued
    # for it — two separate units of work, as the real use cases will be.
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.users.add(user)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.refresh_tokens.add(token)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        loaded = await uow.refresh_tokens.get_by_token_hash("hashed-token")

    assert loaded is not None
    assert loaded.id == token.id
    assert loaded.user_id == user.id
    assert loaded.revoked_at is None


async def test_deleting_user_cascades_to_refresh_tokens(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    user = _user()
    token = RefreshToken(
        id=RefreshTokenId.new(),
        user_id=user.id,
        token_hash="hashed-token",
        issued_at=_NOW,
        expires_at=_NOW + dt.timedelta(days=30),
    )

    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.users.add(user)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.refresh_tokens.add(token)
        await uow.commit()

    # Hard-delete the user row directly; the FK ON DELETE CASCADE must sweep the
    # token (DATABASE.md Section 4).
    async with engine.begin() as conn:
        await conn.execute(
            text('DELETE FROM "user" WHERE id = :uid'), {"uid": user.id.value}
        )

    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert await uow.refresh_tokens.get_by_token_hash("hashed-token") is None
