"""A stale user write must not undo a concurrently committed one (#3214).

Each test pauses one use case right after it reads the target user, commits a
different use case on a second connection, then resumes the paused one. Every
user write is operation-specific, so the resumed writer persists only the columns
it owns: a profile edit cannot restore ``active``, ``is_platform_admin`` or the
password hash it read before an administrator action committed, and a grant
cannot reactivate an account deactivated after it read it. (A deactivation reads
its target under lock, #3239, so a grant cannot commit between its read and its
write.) The password change additionally
compares the hash it verified against, so a stale change cannot replace a password
that changed since.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (a real PostgreSQL); skipped
otherwise (TESTING.md Section 5), mirroring ``test_identity_repositories.py``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.identity.adapters.repositories import (
    SqlAlchemyUserRepository,
)
from mc_server_dashboard_api.identity.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.identity.application.change_password import ChangePassword
from mc_server_dashboard_api.identity.application.set_platform_admin import (
    SetPlatformAdmin,
)
from mc_server_dashboard_api.identity.application.set_user_active import (
    SetUserActive,
)
from mc_server_dashboard_api.identity.application.update_profile import (
    UpdateProfile,
)
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import InvalidCredentialsError
from mc_server_dashboard_api.identity.domain.password_policy import PasswordPolicy
from mc_server_dashboard_api.identity.domain.value_objects import UserId, Username
from tests.identity.fakes import FakeClock, StubHasher, make_user
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_CURRENT = "Wm7!qz#Lp2vT"
_NEW = "Np4@xZ#Lq9wR"
_OTHER = "Kd8$vB#Rt3yQ"


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


class _Pause:
    """Holds a use case right after its first user read until released."""

    def __init__(self) -> None:
        self.read_done = asyncio.Event()
        self.resume = asyncio.Event()


class _PausingUserRepository(SqlAlchemyUserRepository):
    def __init__(self, session: AsyncSession, pause: _Pause) -> None:
        super().__init__(session)
        self._pause = pause

    async def get_by_id(self, user_id: UserId) -> User | None:
        user = await super().get_by_id(user_id)
        self._pause.read_done.set()
        await self._pause.resume.wait()
        return user


class _PausingUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], pause: _Pause
    ) -> None:
        super().__init__(session_factory)
        self._pause = pause

    async def __aenter__(self) -> _PausingUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        self.users = _PausingUserRepository(self._session, self._pause)
        return self


def _policy() -> PasswordPolicy:
    return PasswordPolicy(
        min_length=12,
        max_length=128,
        max_bytes=None,
        require_complexity=True,
        complexity_classes=3,
        check_common_list=True,
        forbid_user_info=True,
        forbid_simple_patterns=True,
        common_passwords=frozenset({"password"}),
    )


def _change_password(uow: SqlAlchemyUnitOfWork) -> ChangePassword:
    return ChangePassword(
        uow=uow, hasher=StubHasher(), clock=FakeClock(_NOW), policy=_policy()
    )


async def _seed(factory: async_sessionmaker[AsyncSession], *users: User) -> None:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        for user in users:
            await uow.users.add(user)
        await uow.commit()


async def _load(factory: async_sessionmaker[AsyncSession], user_id: UserId) -> User:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        user = await uow.users.get_by_id(user_id)
    assert user is not None
    return user


async def _stale_rename(
    factory: async_sessionmaker[AsyncSession], target: UserId, pause: _Pause
) -> asyncio.Task[User]:
    """Start a name-only profile edit and return once it has read the target."""

    use_case = UpdateProfile(
        uow=_PausingUnitOfWork(factory, pause), clock=FakeClock(_NOW)
    )
    task = asyncio.create_task(use_case(user_id=target, username="renamed", email=None))
    await pause.read_done.wait()
    return task


async def test_stale_profile_edit_preserves_deactivation(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    actor = make_user(
        username="actor", email="actor@example.com", is_platform_admin=True
    )
    target = make_user(
        username="target", email="target@example.com", is_platform_admin=True
    )
    await _seed(factory, actor, target)

    pause = _Pause()
    stale = await _stale_rename(factory, target.id, pause)
    await SetUserActive(uow=SqlAlchemyUnitOfWork(factory), clock=FakeClock(_NOW))(
        actor_id=actor.id, target_id=target.id, active=False
    )
    pause.resume.set()
    returned = await stale

    persisted = await _load(factory, target.id)
    assert persisted.active is False
    assert persisted.username == Username("renamed")
    # The response reflects the persisted row, not the stale read.
    assert returned.active is False


async def test_stale_profile_edit_preserves_admin_revocation(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    keep = make_user(username="keep", email="keep@example.com", is_platform_admin=True)
    target = make_user(
        username="target", email="target@example.com", is_platform_admin=True
    )
    await _seed(factory, keep, target)

    pause = _Pause()
    stale = await _stale_rename(factory, target.id, pause)
    await SetPlatformAdmin(uow=SqlAlchemyUnitOfWork(factory), clock=FakeClock(_NOW))(
        target_id=target.id, grant=False
    )
    pause.resume.set()
    returned = await stale

    persisted = await _load(factory, target.id)
    assert persisted.is_platform_admin is False
    assert persisted.username == Username("renamed")
    assert returned.is_platform_admin is False


async def test_stale_profile_edit_preserves_password_change(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    target = make_user(username="target", email="target@example.com", password=_CURRENT)
    await _seed(factory, target)

    pause = _Pause()
    stale = await _stale_rename(factory, target.id, pause)
    await _change_password(SqlAlchemyUnitOfWork(factory))(
        user_id=target.id, current_password=_CURRENT, new_password=_NEW
    )
    pause.resume.set()
    returned = await stale

    persisted = await _load(factory, target.id)
    assert persisted.password_hash == f"hashed::{_NEW}"
    assert persisted.username == Username("renamed")
    assert returned.password_hash == f"hashed::{_NEW}"


async def test_stale_grant_preserves_concurrent_deactivation(
    engine: AsyncEngine,
) -> None:
    # Security operations must not overwrite each other's columns either: a
    # grant that read the target before a deactivation committed leaves the
    # deactivation in place.
    factory = create_session_factory(engine)
    actor = make_user(
        username="actor", email="actor@example.com", is_platform_admin=True
    )
    target = make_user(username="target", email="target@example.com")
    await _seed(factory, actor, target)

    pause = _Pause()
    grant = SetPlatformAdmin(
        uow=_PausingUnitOfWork(factory, pause), clock=FakeClock(_NOW)
    )
    stale = asyncio.create_task(grant(target_id=target.id, grant=True))
    await pause.read_done.wait()
    await SetUserActive(uow=SqlAlchemyUnitOfWork(factory), clock=FakeClock(_NOW))(
        actor_id=actor.id, target_id=target.id, active=False
    )
    pause.resume.set()
    await stale

    persisted = await _load(factory, target.id)
    assert persisted.active is False
    assert persisted.is_platform_admin is True


async def test_stale_password_change_is_refused_after_a_concurrent_change(
    engine: AsyncEngine,
) -> None:
    # The current-password check depends on the hash read before the write; a
    # change that verified against a since-replaced hash must not overwrite the
    # newer password.
    factory = create_session_factory(engine)
    target = make_user(username="target", email="target@example.com", password=_CURRENT)
    await _seed(factory, target)

    pause = _Pause()
    stale = asyncio.create_task(
        _change_password(_PausingUnitOfWork(factory, pause))(
            user_id=target.id, current_password=_CURRENT, new_password=_OTHER
        )
    )
    await pause.read_done.wait()
    await _change_password(SqlAlchemyUnitOfWork(factory))(
        user_id=target.id, current_password=_CURRENT, new_password=_NEW
    )
    pause.resume.set()
    with pytest.raises(InvalidCredentialsError):
        await stale

    persisted = await _load(factory, target.id)
    assert persisted.password_hash == f"hashed::{_NEW}"
