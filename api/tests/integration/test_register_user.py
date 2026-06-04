"""Integration tests for RegisterUser uniqueness against real PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise so the unit suite stays fast and hermetic (TESTING.md
Section 5). This is the one place the DB unique constraints — and their
translation to domain errors on the race path — are exercised end-to-end.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

from mc_server_dashboard_api.core.adapters.database import (
    create_engine,
    create_session_factory,
)
from mc_server_dashboard_api.identity.adapters.clock import SystemClock
from mc_server_dashboard_api.identity.adapters.password_hasher import (
    Argon2PasswordHasher,
)
from mc_server_dashboard_api.identity.adapters.unit_of_work import SqlAlchemyUnitOfWork
from mc_server_dashboard_api.identity.application.register_user import RegisterUser
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import (
    EmailAlreadyExistsError,
    UsernameAlreadyExistsError,
)
from mc_server_dashboard_api.identity.domain.password_policy import PasswordPolicy
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    UserId,
    Username,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")
_VALID_PASSWORD = "Wm7!qz#Lp2vT"

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)


def _policy() -> PasswordPolicy:
    return PasswordPolicy(
        min_length=12,
        max_length=128,
        require_complexity=True,
        check_common_list=False,
        forbid_user_info=True,
        forbid_simple_patterns=True,
        common_passwords=frozenset(),
    )


_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _alembic_config() -> Config:
    # migrations/env.py reads MCD_API_DATABASE__URL; the conftest fixture points
    # it at the test database, so the migrations build the real schema there.
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ALEMBIC_INI.parent / "migrations"))
    return cfg


@pytest_asyncio.fixture
async def engine(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    # Build the production schema via the migrations (not metadata.create_all),
    # so constraint names and DDL match what the app runs against. The alembic
    # env runs its own asyncio.run, so drive it in a worker thread to avoid
    # nesting it inside the test's running event loop.
    monkeypatch.setenv("MCD_API_DATABASE__URL", _DB_URL)
    config = _alembic_config()
    await asyncio.to_thread(command.downgrade, config, "base")
    await asyncio.to_thread(command.upgrade, config, "head")
    eng = create_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await asyncio.to_thread(command.downgrade, config, "base")


def _register(engine: AsyncEngine) -> RegisterUser:
    return RegisterUser(
        uow=SqlAlchemyUnitOfWork(create_session_factory(engine)),
        hasher=Argon2PasswordHasher(),
        clock=SystemClock(),
        policy=_policy(),
    )


async def test_registers_and_persists(engine: AsyncEngine) -> None:
    user = await _register(engine)(
        username="alice", email="alice@example.com", password=_VALID_PASSWORD
    )
    assert user.password_hash != _VALID_PASSWORD


async def test_duplicate_username_case_insensitive_raises(engine: AsyncEngine) -> None:
    register = _register(engine)
    await register(
        username="alice", email="alice@example.com", password=_VALID_PASSWORD
    )
    with pytest.raises(UsernameAlreadyExistsError):
        await register(
            username="ALICE", email="other@example.com", password=_VALID_PASSWORD
        )


async def test_duplicate_email_raises(engine: AsyncEngine) -> None:
    register = _register(engine)
    await register(
        username="alice", email="alice@example.com", password=_VALID_PASSWORD
    )
    with pytest.raises(EmailAlreadyExistsError):
        await register(
            username="bob", email="alice@example.com", password=_VALID_PASSWORD
        )


async def test_unit_of_work_translates_username_constraint_violation(
    engine: AsyncEngine,
) -> None:
    # Insert directly through the unit of work, skipping the use case's pre-check,
    # so the DB unique constraint (not the pre-check) is what trips — the race
    # path. The constraint violation must surface as the domain error.
    factory = create_session_factory(engine)
    now = dt.datetime.now(tz=dt.timezone.utc)

    def _user(username: str, email: str) -> User:
        return User(
            id=UserId.new(),
            username=Username(username),
            email=EmailAddress(email),
            password_hash="x",
            created_at=now,
            updated_at=now,
        )

    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.users.add(_user("alice", "alice@example.com"))
        await uow.commit()

    with pytest.raises(UsernameAlreadyExistsError):
        async with SqlAlchemyUnitOfWork(factory) as uow:
            await uow.users.add(_user("ALICE", "different@example.com"))
            await uow.commit()
