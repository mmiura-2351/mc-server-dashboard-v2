"""Unit tests for the DeleteAccount use case (self-service, FR-COMM-4/FR-AUTH-6).

Drives the use case against in-memory fakes (TESTING.md Section 4). Verifies the
refusals (community owner, last platform admin) and the happy path: the user row
is deleted, their refresh tokens revoked, and the change committed. Memberships
and grants are removed by DB cascade, so they are not asserted here.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from mc_server_dashboard_api.identity.application.delete_account import DeleteAccount
from mc_server_dashboard_api.identity.domain.entities import RefreshToken
from mc_server_dashboard_api.identity.domain.errors import (
    CommunityOwnedError,
    InvalidCredentialsError,
    LastPlatformAdminError,
    UserNotFoundError,
)
from mc_server_dashboard_api.identity.domain.value_objects import (
    RefreshTokenId,
    RotationChainId,
    UserId,
)
from tests.identity.fakes import (
    FakeClock,
    FakeCommunityOwnership,
    FakeUnitOfWork,
    FakeUserRepository,
    StubHasher,
    make_user,
)

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
_PASSWORD = "Wm7!qz#Lp2vT"


def _use_case(uow: FakeUnitOfWork, ownership: FakeCommunityOwnership) -> DeleteAccount:
    # The ownership seam is bound on the unit of work's transaction (#3217).
    uow.ownership = ownership
    return DeleteAccount(uow=uow, hasher=StubHasher(), clock=FakeClock(_NOW))


def _active_token(user_id: UserId) -> RefreshToken:
    return RefreshToken(
        id=RefreshTokenId.new(),
        user_id=user_id,
        chain_id=RotationChainId.new(),
        token_hash=f"hash::{user_id.value}",
        issued_at=_NOW,
        expires_at=_NOW + dt.timedelta(days=14),
    )


async def test_delete_account_removes_user_and_revokes_tokens() -> None:
    user = make_user(now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    uow.refresh_tokens.seed(_active_token(user.id))

    await _use_case(uow, FakeCommunityOwnership())(user_id=user.id, password=_PASSWORD)

    assert user.id not in uow.users.by_id
    assert all(t.revoked_at == _NOW for t in uow.refresh_tokens.by_hash.values())
    assert uow.commits == 1


async def test_delete_account_refused_when_user_owns_a_community() -> None:
    user = make_user(now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    ownership = FakeCommunityOwnership(owners={user.id})

    with pytest.raises(CommunityOwnedError):
        await _use_case(uow, ownership)(user_id=user.id, password=_PASSWORD)
    assert user.id in uow.users.by_id
    assert uow.commits == 0


async def test_delete_account_refused_when_last_platform_admin() -> None:
    admin = make_user(now=_NOW)
    admin.is_platform_admin = True
    uow = FakeUnitOfWork()
    uow.users.seed(admin)

    with pytest.raises(LastPlatformAdminError):
        await _use_case(uow, FakeCommunityOwnership())(
            user_id=admin.id, password=_PASSWORD
        )
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

    await _use_case(uow, FakeCommunityOwnership())(user_id=admin.id, password=_PASSWORD)

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
        await _use_case(uow, FakeCommunityOwnership())(
            user_id=admin.id, password=_PASSWORD
        )
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

    await _use_case(uow, FakeCommunityOwnership())(user_id=admin.id, password=_PASSWORD)

    assert uow.users.lock_calls == 1


async def test_delete_account_non_admin_locks_active_admins() -> None:
    # The guard decides from the account as locked with the admin set (#3239):
    # an unlocked read of "not an admin" may be stale by the time it deletes.
    user = make_user(now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)

    await _use_case(uow, FakeCommunityOwnership())(user_id=user.id, password=_PASSWORD)

    assert uow.users.lock_calls == 1


async def test_delete_account_of_deactivated_admin_allowed() -> None:
    # An admin deactivated after authenticating no longer counts toward the
    # active-admin invariant, so deleting the account cannot reduce it (#3239).
    admin = make_user(now=_NOW, is_platform_admin=True, active=False)
    uow = FakeUnitOfWork()
    uow.users.seed(admin)

    await _use_case(uow, FakeCommunityOwnership())(user_id=admin.id, password=_PASSWORD)

    assert admin.id not in uow.users.by_id


async def test_delete_account_unknown_user_raises() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(UserNotFoundError):
        await _use_case(uow, FakeCommunityOwnership())(
            user_id=UserId.new(), password=_PASSWORD
        )


async def test_delete_account_wrong_password_raises_invalid_credentials() -> None:
    # Re-auth gates before any deletion or refusal-reason leak (issue #420): a
    # wrong password is the same InvalidCredentialsError login raises.
    user = make_user(now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)

    with pytest.raises(InvalidCredentialsError):
        await _use_case(uow, FakeCommunityOwnership())(
            user_id=user.id, password="not-the-password"
        )
    assert user.id in uow.users.by_id
    assert uow.commits == 0


async def test_delete_account_wrong_password_gates_before_owner_check() -> None:
    # Re-auth runs before the community-owner refusal, so a wrong password never
    # reveals whether the account would otherwise be blocked from deletion.
    user = make_user(now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    ownership = FakeCommunityOwnership(owners={user.id})

    with pytest.raises(InvalidCredentialsError):
        await _use_case(uow, ownership)(user_id=user.id, password="not-the-password")


class _PasswordChangedDuringVerify(StubHasher):
    """Replaces the stored hash once the password has verified against it.

    Stands in for a password change that commits after the verify and before
    the deletion locks the account.
    """

    def __init__(self, users: FakeUserRepository, user_id: UserId) -> None:
        super().__init__()
        self._users = users
        self._user_id = user_id

    async def verify(self, plaintext: str, password_hash: str) -> bool:
        verified = await super().verify(plaintext, password_hash)
        stored = self._users.by_id[self._user_id]
        self._users.by_id[self._user_id] = replace(
            stored, password_hash="hashed::Np4@xZ#Lq9wR"
        )
        return verified


@pytest.mark.parametrize("owns_community", [False, True], ids=["plain", "owner"])
async def test_delete_account_refused_when_the_password_changed_after_the_verify(
    owns_community: bool,
) -> None:
    # The password is verified outside the deletion's transaction (#3861), so a
    # change can commit before the account is locked. The verify is then stale
    # and the deletion is refused like a wrong password, ahead of the owner
    # check, and the account and its sessions survive.
    user = make_user(now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    uow.refresh_tokens.seed(_active_token(user.id))
    uow.ownership = FakeCommunityOwnership(owners={user.id} if owns_community else None)
    use_case = DeleteAccount(
        uow=uow,
        hasher=_PasswordChangedDuringVerify(uow.users, user.id),
        clock=FakeClock(_NOW),
    )

    with pytest.raises(InvalidCredentialsError):
        await use_case(user_id=user.id, password=_PASSWORD)

    assert user.id in uow.users.by_id
    assert all(t.revoked_at is None for t in uow.refresh_tokens.by_hash.values())
    assert uow.commits == 0
