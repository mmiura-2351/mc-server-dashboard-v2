"""Unit tests for the SetUserActive use case (admin deactivate / reactivate, #278).

Drives the use case against in-memory fakes (TESTING.md Section 4). Verifies the
deactivate refusals (self, last active admin), token revocation on deactivate,
and the reactivate happy path.
"""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.identity.application.set_user_active import SetUserActive
from mc_server_dashboard_api.identity.domain.entities import RefreshToken
from mc_server_dashboard_api.identity.domain.errors import (
    LastPlatformAdminError,
    SelfTargetError,
    UserNotFoundError,
)
from mc_server_dashboard_api.identity.domain.value_objects import (
    RefreshTokenId,
    UserId,
)
from tests.identity.fakes import FakeClock, FakeUnitOfWork, make_user

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)


def _use_case(uow: FakeUnitOfWork) -> SetUserActive:
    return SetUserActive(uow=uow, clock=FakeClock(_NOW))


def _active_token(user_id: UserId) -> RefreshToken:
    return RefreshToken(
        id=RefreshTokenId.new(),
        user_id=user_id,
        token_hash=f"hash::{user_id.value}",
        issued_at=_NOW,
        expires_at=_NOW + dt.timedelta(days=14),
    )


async def test_deactivate_sets_flag_and_revokes_tokens() -> None:
    admin = make_user(username="admin", is_platform_admin=True)
    target = make_user(username="bob", email="bob@example.com")
    uow = FakeUnitOfWork()
    uow.users.seed(admin)
    uow.users.seed(target)
    uow.refresh_tokens.seed(_active_token(target.id))

    await _use_case(uow)(actor_id=admin.id, target_id=target.id, active=False)

    assert uow.users.by_id[target.id].active is False
    assert all(t.revoked_at == _NOW for t in uow.refresh_tokens.by_hash.values())
    assert uow.commits == 1


async def test_deactivate_self_refused() -> None:
    admin = make_user(username="admin", is_platform_admin=True)
    other = make_user(
        username="other", email="other@example.com", is_platform_admin=True
    )
    uow = FakeUnitOfWork()
    uow.users.seed(admin)
    uow.users.seed(other)

    with pytest.raises(SelfTargetError):
        await _use_case(uow)(actor_id=admin.id, target_id=admin.id, active=False)
    assert uow.users.by_id[admin.id].active is True
    assert uow.commits == 0


async def test_deactivate_last_active_admin_refused() -> None:
    # The actor is a non-admin and the target is the only active admin, so
    # deactivating it would leave zero active admins -- refused.
    actor = make_user(username="actor", email="actor@example.com")
    target = make_user(
        username="solo", email="solo@example.com", is_platform_admin=True
    )
    uow = FakeUnitOfWork()
    uow.users.seed(actor)
    uow.users.seed(target)

    with pytest.raises(LastPlatformAdminError):
        await _use_case(uow)(actor_id=actor.id, target_id=target.id, active=False)
    assert uow.users.by_id[target.id].active is True
    assert uow.commits == 0


async def test_deactivate_admin_allowed_when_other_active_admin_remains() -> None:
    actor = make_user(username="actor", is_platform_admin=True)
    target = make_user(username="t", email="t@example.com", is_platform_admin=True)
    keep = make_user(username="keep", email="keep@example.com", is_platform_admin=True)
    uow = FakeUnitOfWork()
    uow.users.seed(actor)
    uow.users.seed(target)
    uow.users.seed(keep)

    await _use_case(uow)(actor_id=actor.id, target_id=target.id, active=False)

    assert uow.users.by_id[target.id].active is False


async def test_deactivate_admin_locks_active_admins() -> None:
    # Deactivating an active admin reduces the set, so the FOR UPDATE lock is
    # taken to serialize concurrent last-two-admin deactivations (#260).
    actor = make_user(username="actor", is_platform_admin=True)
    target = make_user(username="t", email="t@example.com", is_platform_admin=True)
    keep = make_user(username="keep", email="keep@example.com", is_platform_admin=True)
    uow = FakeUnitOfWork()
    uow.users.seed(actor)
    uow.users.seed(target)
    uow.users.seed(keep)

    await _use_case(uow)(actor_id=actor.id, target_id=target.id, active=False)

    assert uow.users.lock_calls == 1


async def test_reactivate_does_not_lock_active_admins() -> None:
    # Reactivation never reduces the active-admin set, so it stays lock-free (#260).
    admin = make_user(username="admin", is_platform_admin=True)
    target = make_user(username="bob", email="bob@example.com", active=False)
    uow = FakeUnitOfWork()
    uow.users.seed(admin)
    uow.users.seed(target)

    await _use_case(uow)(actor_id=admin.id, target_id=target.id, active=True)

    assert uow.users.lock_calls == 0


async def test_reactivate_sets_flag_without_revoking() -> None:
    admin = make_user(username="admin", is_platform_admin=True)
    target = make_user(username="bob", email="bob@example.com", active=False)
    uow = FakeUnitOfWork()
    uow.users.seed(admin)
    uow.users.seed(target)

    await _use_case(uow)(actor_id=admin.id, target_id=target.id, active=True)

    assert uow.users.by_id[target.id].active is True
    assert uow.commits == 1


async def test_unknown_target_raises() -> None:
    admin = make_user(username="admin", is_platform_admin=True)
    uow = FakeUnitOfWork()
    uow.users.seed(admin)

    with pytest.raises(UserNotFoundError):
        await _use_case(uow)(actor_id=admin.id, target_id=UserId.new(), active=False)
