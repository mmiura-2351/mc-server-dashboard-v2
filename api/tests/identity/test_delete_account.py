"""Unit tests for the DeleteAccount use case (self-service, FR-COMM-4/FR-AUTH-6).

Drives the use case against in-memory fakes (TESTING.md Section 4). Verifies the
refusals (community owner, last platform admin) and the happy path: the user row
is deleted, their refresh tokens revoked, and the change committed. Memberships
and grants are removed by DB cascade, so they are not asserted here.
"""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.identity.application.delete_account import DeleteAccount
from mc_server_dashboard_api.identity.domain.entities import RefreshToken
from mc_server_dashboard_api.identity.domain.errors import (
    CommunityOwnedError,
    LastPlatformAdminError,
    UserNotFoundError,
)
from mc_server_dashboard_api.identity.domain.value_objects import (
    RefreshTokenId,
    UserId,
)
from tests.identity.fakes import (
    FakeClock,
    FakeCommunityOwnership,
    FakeUnitOfWork,
    make_user,
)

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)


def _use_case(uow: FakeUnitOfWork, ownership: FakeCommunityOwnership) -> DeleteAccount:
    return DeleteAccount(uow=uow, ownership=ownership, clock=FakeClock(_NOW))


def _active_token(user_id: UserId) -> RefreshToken:
    return RefreshToken(
        id=RefreshTokenId.new(),
        user_id=user_id,
        token_hash=f"hash::{user_id.value}",
        issued_at=_NOW,
        expires_at=_NOW + dt.timedelta(days=14),
    )


async def test_delete_account_removes_user_and_revokes_tokens() -> None:
    user = make_user(now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    uow.refresh_tokens.seed(_active_token(user.id))

    await _use_case(uow, FakeCommunityOwnership())(user_id=user.id)

    assert user.id not in uow.users.by_id
    assert all(t.revoked_at == _NOW for t in uow.refresh_tokens.by_hash.values())
    assert uow.commits == 1


async def test_delete_account_refused_when_user_owns_a_community() -> None:
    user = make_user(now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    ownership = FakeCommunityOwnership(owners={user.id})

    with pytest.raises(CommunityOwnedError):
        await _use_case(uow, ownership)(user_id=user.id)
    assert user.id in uow.users.by_id
    assert uow.commits == 0


async def test_delete_account_refused_when_last_platform_admin() -> None:
    admin = make_user(now=_NOW)
    admin.is_platform_admin = True
    uow = FakeUnitOfWork()
    uow.users.seed(admin)

    with pytest.raises(LastPlatformAdminError):
        await _use_case(uow, FakeCommunityOwnership())(user_id=admin.id)
    assert admin.id in uow.users.by_id
    assert uow.commits == 0


async def test_delete_account_allowed_when_other_admin_remains() -> None:
    admin = make_user(username="a", email="a@example.com", now=_NOW)
    admin.is_platform_admin = True
    other = make_user(username="b", email="b@example.com", now=_NOW)
    other.is_platform_admin = True
    uow = FakeUnitOfWork()
    uow.users.seed(admin)
    uow.users.seed(other)

    await _use_case(uow, FakeCommunityOwnership())(user_id=admin.id)

    assert admin.id not in uow.users.by_id


async def test_delete_account_refused_when_only_other_admin_is_deactivated() -> None:
    # A deactivated admin does not count toward the last-active-admin invariant
    # (#278), so the active admin is still the last one and cannot self-delete.
    admin = make_user(username="a", email="a@example.com", now=_NOW)
    admin.is_platform_admin = True
    deactivated = make_user(username="b", email="b@example.com", now=_NOW)
    deactivated.is_platform_admin = True
    deactivated.active = False
    uow = FakeUnitOfWork()
    uow.users.seed(admin)
    uow.users.seed(deactivated)

    with pytest.raises(LastPlatformAdminError):
        await _use_case(uow, FakeCommunityOwnership())(user_id=admin.id)
    assert admin.id in uow.users.by_id


async def test_delete_account_admin_locks_active_admins() -> None:
    # An admin self-delete reduces the active-admin set, so the FOR UPDATE lock
    # is taken to serialize concurrent last-two-admin self-deletes (#260).
    admin = make_user(username="a", email="a@example.com", now=_NOW)
    admin.is_platform_admin = True
    other = make_user(username="b", email="b@example.com", now=_NOW)
    other.is_platform_admin = True
    uow = FakeUnitOfWork()
    uow.users.seed(admin)
    uow.users.seed(other)

    await _use_case(uow, FakeCommunityOwnership())(user_id=admin.id)

    assert uow.users.lock_calls == 1


async def test_delete_account_non_admin_does_not_lock_active_admins() -> None:
    # A non-admin self-delete never reduces the active-admin set, so it stays
    # lock-free (#260).
    user = make_user(now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)

    await _use_case(uow, FakeCommunityOwnership())(user_id=user.id)

    assert uow.users.lock_calls == 0


async def test_delete_account_unknown_user_raises() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(UserNotFoundError):
        await _use_case(uow, FakeCommunityOwnership())(user_id=UserId.new())
