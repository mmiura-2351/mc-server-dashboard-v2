"""Concurrency test for the at-least-one-active-admin invariant (#260).

Proves the FOR UPDATE lock in ``lock_active_platform_admins`` closes the
last-two-admin race: two concurrent transactions each revoke one of the only two
active platform admins. Without the lock both would read a count of 2, pass the
guard, and commit -- leaving zero admins. With it, the two transactions serialize
on the locked admin rows and exactly one succeeds; the other re-counts the
decremented set and refuses with :class:`LastPlatformAdminError`.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (a real PostgreSQL); skipped
otherwise (TESTING.md Section 5), mirroring ``test_identity_repositories.py``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.identity.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.identity.application.set_platform_admin import (
    SetPlatformAdmin,
)
from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import LastPlatformAdminError
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

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


class _FixedClock(Clock):
    def now(self) -> dt.datetime:
        return _NOW


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


def _admin(username: str, email: str) -> User:
    return User(
        id=UserId.new(),
        username=Username(username),
        email=EmailAddress(email),
        password_hash="hash",
        created_at=_NOW,
        updated_at=_NOW,
        is_platform_admin=True,
        active=True,
    )


async def test_concurrent_last_two_admin_revokes_leave_exactly_one(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    admin_a = _admin("admin_a", "admin_a@example.com")
    admin_b = _admin("admin_b", "admin_b@example.com")
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.users.add(admin_a)
        await uow.users.add(admin_b)
        await uow.commit()

    async def revoke(target: UserId) -> bool:
        # A fresh UnitOfWork == a fresh session/transaction per racer, matching
        # how two concurrent requests would run. Returns True if the revoke
        # committed, False if the invariant refused it.
        use_case = SetPlatformAdmin(
            uow=SqlAlchemyUnitOfWork(factory), clock=_FixedClock()
        )
        try:
            await use_case(target_id=target, grant=False)
        except LastPlatformAdminError:
            return False
        return True

    results = await asyncio.gather(revoke(admin_a.id), revoke(admin_b.id))

    # Exactly one of the two concurrent revokes wins; the other is refused.
    assert sorted(results) == [False, True]

    async with SqlAlchemyUnitOfWork(factory) as uow:
        remaining = await uow.users.count_active_platform_admins()
    assert remaining == 1
