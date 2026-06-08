"""Unit tests for the ChangePassword use case (self-service, FR-AUTH-4).

Drives the use case against in-memory fakes (no DB, no JWT lib; TESTING.md
Section 4). Verifies: the current password is verified (uniform failure on a
mismatch), the new password passes the full registration policy with the same
reason codes, the stored hash is replaced, all of the user's refresh tokens are
revoked, and the change commits atomically.
"""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.identity.application.change_password import ChangePassword
from mc_server_dashboard_api.identity.domain.entities import RefreshToken
from mc_server_dashboard_api.identity.domain.errors import (
    InvalidCredentialsError,
    PasswordPolicyError,
)
from mc_server_dashboard_api.identity.domain.password_policy import PasswordPolicy
from mc_server_dashboard_api.identity.domain.value_objects import RefreshTokenId
from tests.identity.fakes import (
    FakeClock,
    FakeUnitOfWork,
    StubHasher,
    make_user,
)

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
_CURRENT = "Wm7!qz#Lp2vT"
_NEW = "Np4@xZ#Lq9wR"


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


def _use_case(uow: FakeUnitOfWork) -> ChangePassword:
    return ChangePassword(
        uow=uow,
        hasher=StubHasher(),
        clock=FakeClock(_NOW),
        policy=_policy(),
    )


def _active_token(user_id: object, *, suffix: str) -> RefreshToken:
    return RefreshToken(
        id=RefreshTokenId.new(),
        user_id=user_id,  # type: ignore[arg-type]
        token_hash=f"hash::{suffix}",
        issued_at=_NOW,
        expires_at=_NOW + dt.timedelta(days=14),
    )


async def test_change_password_rehashes_and_commits() -> None:
    user = make_user(password=_CURRENT, now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)

    await _use_case(uow)(user_id=user.id, current_password=_CURRENT, new_password=_NEW)

    stored = uow.users.by_id[user.id]
    assert stored.password_hash == f"hashed::{_NEW}"
    assert uow.commits == 1


async def test_change_password_wrong_current_raises_invalid_credentials() -> None:
    user = make_user(password=_CURRENT, now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)

    with pytest.raises(InvalidCredentialsError):
        await _use_case(uow)(
            user_id=user.id, current_password="not-the-password", new_password=_NEW
        )
    # The stored hash is untouched and nothing committed.
    assert uow.users.by_id[user.id].password_hash == f"hashed::{_CURRENT}"
    assert uow.commits == 0


async def test_change_password_weak_new_password_raises_policy_error() -> None:
    user = make_user(password=_CURRENT, now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)

    with pytest.raises(PasswordPolicyError) as exc:
        await _use_case(uow)(
            user_id=user.id, current_password=_CURRENT, new_password="short"
        )
    assert exc.value.reason == "too_short"
    assert uow.commits == 0


async def test_change_password_revokes_all_refresh_tokens() -> None:
    user = make_user(password=_CURRENT, now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    uow.refresh_tokens.seed(_active_token(user.id, suffix="a"))
    uow.refresh_tokens.seed(_active_token(user.id, suffix="b"))

    await _use_case(uow)(user_id=user.id, current_password=_CURRENT, new_password=_NEW)

    assert all(
        token.revoked_at == _NOW for token in uow.refresh_tokens.by_hash.values()
    )
