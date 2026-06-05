"""Unit tests for the SetPlatformAdmin use case (grant / revoke admin, #278)."""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.identity.application.set_platform_admin import (
    SetPlatformAdmin,
)
from mc_server_dashboard_api.identity.domain.errors import (
    LastPlatformAdminError,
    UserNotFoundError,
)
from mc_server_dashboard_api.identity.domain.value_objects import UserId
from tests.identity.fakes import FakeClock, FakeUnitOfWork, make_user

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)


def _use_case(uow: FakeUnitOfWork) -> SetPlatformAdmin:
    return SetPlatformAdmin(uow=uow, clock=FakeClock(_NOW))


async def test_grant_sets_flag() -> None:
    target = make_user(username="bob", email="bob@example.com")
    uow = FakeUnitOfWork()
    uow.users.seed(target)

    await _use_case(uow)(target_id=target.id, grant=True)

    assert uow.users.by_id[target.id].is_platform_admin is True
    assert uow.commits == 1


async def test_grant_does_not_lock_active_admins() -> None:
    # A grant never reduces the active-admin set, so it must stay lock-free (#260).
    target = make_user(username="bob", email="bob@example.com")
    uow = FakeUnitOfWork()
    uow.users.seed(target)

    await _use_case(uow)(target_id=target.id, grant=True)

    assert uow.users.lock_calls == 0


async def test_revoke_locks_active_admins() -> None:
    # Revoking an active admin reduces the set, so the FOR UPDATE lock is taken
    # to serialize concurrent last-two-admin revokes (#260).
    target = make_user(username="t", email="t@example.com", is_platform_admin=True)
    keep = make_user(username="keep", email="keep@example.com", is_platform_admin=True)
    uow = FakeUnitOfWork()
    uow.users.seed(target)
    uow.users.seed(keep)

    await _use_case(uow)(target_id=target.id, grant=False)

    assert uow.users.lock_calls == 1


async def test_revoke_sets_flag_when_other_admin_remains() -> None:
    target = make_user(username="t", email="t@example.com", is_platform_admin=True)
    keep = make_user(username="keep", email="keep@example.com", is_platform_admin=True)
    uow = FakeUnitOfWork()
    uow.users.seed(target)
    uow.users.seed(keep)

    await _use_case(uow)(target_id=target.id, grant=False)

    assert uow.users.by_id[target.id].is_platform_admin is False


async def test_revoke_last_active_admin_refused() -> None:
    target = make_user(
        username="solo", email="solo@example.com", is_platform_admin=True
    )
    deactivated = make_user(
        username="dead", email="dead@example.com", is_platform_admin=True, active=False
    )
    uow = FakeUnitOfWork()
    uow.users.seed(target)
    # A deactivated admin does not keep the invariant satisfied, so revoking the
    # only ACTIVE admin is still refused.
    uow.users.seed(deactivated)

    with pytest.raises(LastPlatformAdminError):
        await _use_case(uow)(target_id=target.id, grant=False)
    assert uow.users.by_id[target.id].is_platform_admin is True
    assert uow.commits == 0


async def test_unknown_target_raises() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(UserNotFoundError):
        await _use_case(uow)(target_id=UserId.new(), grant=True)
