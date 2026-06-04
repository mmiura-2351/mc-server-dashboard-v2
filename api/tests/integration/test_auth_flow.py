"""Integration tests for the auth use cases against PostgreSQL.

Exercises the refresh-token DB paths end to end with the real SqlAlchemy
UnitOfWork and JWT TokenService: login issues a persisted pair, refresh rotates
it (old token rejected afterwards), and reuse of a rotated token revokes the
family. Runs only when ``MCD_TEST_DATABASE_URL`` is set (CI Postgres service).
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.identity.adapters.clock import SystemClock
from mc_server_dashboard_api.identity.adapters.login_failure_delay import (
    NoOpLoginFailureDelay,
)
from mc_server_dashboard_api.identity.adapters.password_hasher import (
    Argon2PasswordHasher,
)
from mc_server_dashboard_api.identity.adapters.token_service import JwtTokenService
from mc_server_dashboard_api.identity.adapters.unit_of_work import SqlAlchemyUnitOfWork
from mc_server_dashboard_api.identity.application.login import Login
from mc_server_dashboard_api.identity.application.refresh_session import RefreshSession
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import InvalidRefreshTokenError
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    UserId,
    Username,
)
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_PASSWORD = "Wm7!qz#Lp2vT"
_REFRESH_TTL = dt.timedelta(days=14)


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


def _tokens() -> JwtTokenService:
    return JwtTokenService(
        signing_key="integration-key",
        algorithm="HS256",
        access_ttl=dt.timedelta(minutes=15),
        clock=SystemClock(),
    )


async def _seed_user(engine: AsyncEngine) -> User:
    hasher = Argon2PasswordHasher()
    now = dt.datetime.now(tz=dt.timezone.utc)
    user = User(
        id=UserId.new(),
        username=Username("alice"),
        email=EmailAddress("alice@example.com"),
        password_hash=hasher.hash(_PASSWORD),
        created_at=now,
        updated_at=now,
    )
    factory = create_session_factory(engine)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.users.add(user)
        await uow.commit()
    return user


async def test_login_then_rotate_then_reuse(engine: AsyncEngine) -> None:
    await _seed_user(engine)
    factory = create_session_factory(engine)

    login = Login(
        uow=SqlAlchemyUnitOfWork(factory),
        hasher=Argon2PasswordHasher(),
        tokens=_tokens(),
        clock=SystemClock(),
        failure_delay=NoOpLoginFailureDelay(),
        refresh_ttl=_REFRESH_TTL,
    )
    pair = await login(username="alice", password=_PASSWORD)

    refresh = RefreshSession(
        uow=SqlAlchemyUnitOfWork(factory),
        tokens=_tokens(),
        clock=SystemClock(),
        refresh_ttl=_REFRESH_TTL,
    )
    rotated = await refresh(refresh_token=pair.refresh_token)
    assert rotated.refresh_token != pair.refresh_token

    # The old (now-rotated) token must be rejected.
    with pytest.raises(InvalidRefreshTokenError):
        await refresh(refresh_token=pair.refresh_token)

    # Reuse triggered a family revoke, so the rotated token is dead too.
    with pytest.raises(InvalidRefreshTokenError):
        await refresh(refresh_token=rotated.refresh_token)
