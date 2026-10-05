"""No interleaving of admin-set changes may leave zero active platform admins (#3239).

Each test pauses one use case right before its write -- after every guard it
runs has decided -- then starts competing use cases on other connections one at
a time, waiting until each has either committed or blocked on a lock, and only
then resumes the paused one. The interleaving is therefore explicit, not timed.

The guards decide from the target row as locked together with the active-admin
set, so a competitor that would change what the paused guard decided from waits
for it, and the paused guard never acts on a state that has since changed:

- a deactivation / revoke / deletion of a non-admin cannot be raced by a grant
  of admin on that account plus a removal of the last other admin;
- two removals on different admins serialize, and exactly one wins;
- removing admin from (or deleting) an account deactivated meanwhile is not
  refused, because it no longer reduces the active-admin count;
- the guard's lock does not block the foreign-key check of a token rotation, so
  the rotation cannot deadlock with the guard revoking that user's tokens.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (a real PostgreSQL); skipped
otherwise (TESTING.md Section 5), mirroring ``test_identity_stale_user_writes.py``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.identity.adapters.repositories import (
    SqlAlchemyUserRepository,
)
from mc_server_dashboard_api.identity.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.identity.application.admin_delete_user import (
    AdminDeleteUser,
)
from mc_server_dashboard_api.identity.application.delete_account import (
    DeleteAccount,
)
from mc_server_dashboard_api.identity.application.set_platform_admin import (
    SetPlatformAdmin,
)
from mc_server_dashboard_api.identity.application.set_user_active import (
    SetUserActive,
)
from mc_server_dashboard_api.identity.domain.entities import (
    REVOKED_ROTATED,
    RefreshToken,
)
from mc_server_dashboard_api.identity.domain.errors import LastPlatformAdminError
from mc_server_dashboard_api.identity.domain.value_objects import (
    RefreshTokenId,
    RotationChainId,
    UserId,
)
from tests.identity.fakes import (
    FakeClock,
    FakeCommunityOwnership,
    StubHasher,
    make_user,
)
from tests.integration.races import SETTLE_TIMEOUT, await_settled, race_database

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_PASSWORD = "Wm7!qz#Lp2vT"


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    async with race_database(_DB_URL) as eng:
        yield eng


class _Pause:
    """Holds a use case right before its first user write until released."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.resume = asyncio.Event()

    async def hold(self) -> None:
        if self.reached.is_set():
            return
        self.reached.set()
        await self.resume.wait()


class _PausingUserRepository(SqlAlchemyUserRepository):
    def __init__(self, session: AsyncSession, pause: _Pause) -> None:
        super().__init__(session)
        self._pause = pause

    async def set_active(
        self, user_id: UserId, *, active: bool, updated_at: dt.datetime
    ) -> None:
        await self._pause.hold()
        await super().set_active(user_id, active=active, updated_at=updated_at)

    async def set_platform_admin(
        self, user_id: UserId, *, is_platform_admin: bool, updated_at: dt.datetime
    ) -> None:
        await self._pause.hold()
        await super().set_platform_admin(
            user_id, is_platform_admin=is_platform_admin, updated_at=updated_at
        )

    async def delete(self, user_id: UserId) -> None:
        await self._pause.hold()
        await super().delete(user_id)


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


class _Backend:
    """The backend pid of a competitor's current transaction, once it has one."""

    pid: int | None = None


class _TrackedUnitOfWork(SqlAlchemyUnitOfWork):
    """Records its transaction's backend pid; a use case may open it twice."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], backend: _Backend
    ) -> None:
        super().__init__(session_factory)
        self._backend = backend

    async def __aenter__(self) -> _TrackedUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        self._backend.pid = (
            await self._session.execute(text("SELECT pg_backend_pid()"))
        ).scalar_one()
        return self


_Op = Callable[[SqlAlchemyUnitOfWork, UserId], Coroutine[Any, Any, None]]


async def _grant(uow: SqlAlchemyUnitOfWork, target: UserId) -> None:
    await SetPlatformAdmin(uow=uow, clock=FakeClock(_NOW))(target_id=target, grant=True)


async def _revoke(uow: SqlAlchemyUnitOfWork, target: UserId) -> None:
    await SetPlatformAdmin(uow=uow, clock=FakeClock(_NOW))(
        target_id=target, grant=False
    )


async def _deactivate(uow: SqlAlchemyUnitOfWork, target: UserId) -> None:
    # The acting admin's identity is irrelevant to the guard; only "not the
    # target" matters (the route has already authorized the actor).
    await SetUserActive(uow=uow, clock=FakeClock(_NOW))(
        actor_id=UserId.new(), target_id=target, active=False
    )


async def _admin_delete(uow: SqlAlchemyUnitOfWork, target: UserId) -> None:
    await AdminDeleteUser(
        uow=uow, ownership=FakeCommunityOwnership(), clock=FakeClock(_NOW)
    )(actor_id=UserId.new(), target_id=target)


async def _delete_account(uow: SqlAlchemyUnitOfWork, target: UserId) -> None:
    await DeleteAccount(
        uow=uow,
        ownership=FakeCommunityOwnership(),
        hasher=StubHasher(),
        clock=FakeClock(_NOW),
    )(user_id=target, password=_PASSWORD)


def _token(user_id: UserId, token_hash: str) -> RefreshToken:
    return RefreshToken(
        id=RefreshTokenId.new(),
        user_id=user_id,
        chain_id=RotationChainId.new(),
        token_hash=token_hash,
        issued_at=_NOW,
        expires_at=_NOW + dt.timedelta(days=14),
    )


async def _rotate_token(uow: SqlAlchemyUnitOfWork, user_id: UserId) -> None:
    # A refresh rotation's writes, in its order: revoke the presented token
    # (locking its row), then insert the successor, whose foreign-key check
    # share-locks the user row at commit.
    async with uow:
        await uow.refresh_tokens.revoke(
            "presented", revoked_at=_NOW, reason=REVOKED_ROTATED
        )
        await uow.refresh_tokens.add(_token(user_id, "successor"))
        await uow.commit()


# Every operation that can reduce the active-admin set.
_REDUCERS = {
    "revoke": _revoke,
    "deactivate": _deactivate,
    "admin-delete": _admin_delete,
    "delete-account": _delete_account,
}


@dataclass(frozen=True)
class _Step:
    op: _Op
    target: UserId


async def _interleave(
    engine: AsyncEngine, paused: _Step, competitors: Sequence[_Step]
) -> list[BaseException | None]:
    """Pause ``paused`` before its write, settle each competitor, then resume.

    Returns each step's outcome (``None`` or the exception it raised), paused
    step first.
    """

    factory = create_session_factory(engine)
    pause = _Pause()
    first = asyncio.create_task(
        paused.op(_PausingUnitOfWork(factory, pause), paused.target)
    )
    await asyncio.wait_for(pause.reached.wait(), SETTLE_TIMEOUT)

    tasks = []
    for step in competitors:
        backend = _Backend()
        task = asyncio.create_task(
            step.op(_TrackedUnitOfWork(factory, backend), step.target)
        )
        await await_settled(engine, task, lambda: backend.pid)
        tasks.append(task)

    pause.resume.set()
    outcomes = await asyncio.wait_for(
        asyncio.gather(first, *tasks, return_exceptions=True), SETTLE_TIMEOUT
    )
    # A refusal is the only acceptable failure: anything else (a deadlock, a
    # serialization error) is a bug in the lock order.
    for outcome in outcomes:
        if outcome is not None and not isinstance(outcome, LastPlatformAdminError):
            raise outcome
    return list(outcomes)


async def _seed_admin_and_user(
    engine: AsyncEngine,
) -> tuple[UserId, UserId]:
    """Seed one active admin and one active non-admin; return their ids."""

    admin = make_user(
        username="admin", email="admin@example.com", is_platform_admin=True
    )
    plain = make_user(username="plain", email="plain@example.com")
    async with SqlAlchemyUnitOfWork(create_session_factory(engine)) as uow:
        await uow.users.add(admin)
        await uow.users.add(plain)
        await uow.commit()
    return admin.id, plain.id


async def _seed_two_admins(engine: AsyncEngine) -> tuple[UserId, UserId]:
    first = make_user(
        username="first", email="first@example.com", is_platform_admin=True
    )
    second = make_user(
        username="second", email="second@example.com", is_platform_admin=True
    )
    async with SqlAlchemyUnitOfWork(create_session_factory(engine)) as uow:
        await uow.users.add(first)
        await uow.users.add(second)
        await uow.commit()
    return first.id, second.id


async def _active_admins(engine: AsyncEngine) -> int:
    async with SqlAlchemyUnitOfWork(create_session_factory(engine)) as uow:
        return await uow.users.count_active_platform_admins()


@pytest.mark.parametrize("removal", list(_REDUCERS), ids=list(_REDUCERS))
@pytest.mark.parametrize("paused", list(_REDUCERS), ids=list(_REDUCERS))
async def test_grant_racing_a_non_admin_reducer_cannot_leave_zero_admins(
    engine: AsyncEngine, paused: str, removal: str
) -> None:
    # The paused use case acts on a non-admin. Meanwhile that account is granted
    # admin and the only other admin is removed. Had the paused guard decided
    # from its unlocked read (a non-admin, so nothing to protect), resuming it
    # would remove the last active admin.
    admin, plain = await _seed_admin_and_user(engine)

    await _interleave(
        engine,
        _Step(_REDUCERS[paused], plain),
        [_Step(_grant, plain), _Step(_REDUCERS[removal], admin)],
    )

    assert await _active_admins(engine) >= 1


@pytest.mark.parametrize("competing", list(_REDUCERS), ids=list(_REDUCERS))
@pytest.mark.parametrize("paused", list(_REDUCERS), ids=list(_REDUCERS))
async def test_removals_of_the_last_two_admins_serialize(
    engine: AsyncEngine, paused: str, competing: str
) -> None:
    first, second = await _seed_two_admins(engine)

    outcomes = await _interleave(
        engine,
        _Step(_REDUCERS[paused], first),
        [_Step(_REDUCERS[competing], second)],
    )

    # The competitor waits for the paused removal, then sees one admin left.
    assert outcomes[0] is None
    assert isinstance(outcomes[1], LastPlatformAdminError)
    assert await _active_admins(engine) == 1


@pytest.mark.parametrize(
    ("paused", "competing"),
    [
        ("deactivate", "revoke"),
        ("revoke", "deactivate"),
        ("deactivate", "admin-delete"),
        ("revoke", "admin-delete"),
    ],
)
async def test_removal_after_the_account_stopped_counting_is_not_refused(
    engine: AsyncEngine, paused: str, competing: str
) -> None:
    # Once the paused use case commits, the target no longer counts as an active
    # admin, so the competitor's change to it cannot reduce the count and must
    # not be refused as if it removed the last admin.
    keep, target = await _seed_two_admins(engine)

    outcomes = await _interleave(
        engine,
        _Step(_REDUCERS[paused], target),
        [_Step(_REDUCERS[competing], target)],
    )

    assert outcomes == [None, None]
    assert await _active_admins(engine) == 1
    async with SqlAlchemyUnitOfWork(create_session_factory(engine)) as uow:
        kept = await uow.users.get_by_id(keep)
    assert kept is not None
    assert kept.is_platform_admin
    assert kept.active


async def test_token_rotation_does_not_deadlock_with_a_deactivation(
    engine: AsyncEngine,
) -> None:
    # The paused deactivation holds the user row from its guard; a lock that
    # blocked the rotation's foreign-key check would leave the rotation holding
    # the presented token's row while the deactivation's revocation waits on it.
    _, plain = await _seed_admin_and_user(engine)
    async with SqlAlchemyUnitOfWork(create_session_factory(engine)) as uow:
        await uow.refresh_tokens.add(_token(plain, "presented"))
        await uow.commit()

    await _interleave(engine, _Step(_deactivate, plain), [_Step(_rotate_token, plain)])

    # The rotation committed first, so the deactivation revoked its successor.
    async with SqlAlchemyUnitOfWork(create_session_factory(engine)) as uow:
        assert await uow.refresh_tokens.list_active_for_user(plain, now=_NOW) == []
