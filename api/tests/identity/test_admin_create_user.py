"""Unit tests for the AdminCreateUser use case against faked Ports (#368).

A platform admin provisions an account regardless of the open-registration flag
and without consuming the per-IP cap. The creation path reuses the same
validation, hashing, uniqueness pre-check, and atomic persist as open
registration; this verifies that shared behaviour plus the actor-attributed
result an admin route audits.
"""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.identity.application.admin_create_user import (
    AdminCreateUser,
)
from mc_server_dashboard_api.identity.domain.errors import (
    PasswordPolicyError,
    UsernameAlreadyExistsError,
)
from mc_server_dashboard_api.identity.domain.password_policy import PasswordPolicy
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    UserId,
    Username,
)
from tests.identity.fakes import (
    FakeClock,
    FakeUnitOfWork,
    FakeUserRepository,
    StubHasher,
    make_user,
)

_VALID_PASSWORD = "Wm7!qz#Lp2vT"
_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)


def _policy() -> PasswordPolicy:
    return PasswordPolicy(
        min_length=12,
        max_length=128,
        max_bytes=None,
        require_complexity=True,
        check_common_list=True,
        forbid_user_info=True,
        forbid_simple_patterns=True,
        common_passwords=frozenset(),
    )


def _use_case(uow: FakeUnitOfWork) -> AdminCreateUser:
    return AdminCreateUser(
        uow=uow,
        hasher=StubHasher(),
        clock=FakeClock(_NOW),
        policy=_policy(),
    )


async def test_creates_user_persists_hash_not_plaintext() -> None:
    uow = FakeUnitOfWork()
    user = await _use_case(uow)(
        username="alice", email="alice@example.com", password=_VALID_PASSWORD
    )
    assert user.username.value == "alice"
    assert user.email.value == "alice@example.com"
    assert user.password_hash == f"hashed::{_VALID_PASSWORD}"
    assert user.created_at == _NOW
    assert uow.commits == 1
    assert uow.users.by_id[user.id] is user


async def test_rejects_weak_password_before_persisting() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(PasswordPolicyError) as exc:
        await _use_case(uow)(
            username="alice", email="alice@example.com", password="short"
        )
    assert exc.value.reason == "too_short"
    assert uow.users.by_id == {}


async def test_rejects_duplicate_username() -> None:
    repo = FakeUserRepository()
    repo.seed(
        make_user(username="alice", email="other@example.com"),
    )
    uow = FakeUnitOfWork(users=repo)
    with pytest.raises(UsernameAlreadyExistsError):
        await _use_case(uow)(
            username="alice", email="alice@example.com", password=_VALID_PASSWORD
        )


async def test_created_account_is_not_platform_admin() -> None:
    uow = FakeUnitOfWork()
    user = await _use_case(uow)(
        username="alice", email="alice@example.com", password=_VALID_PASSWORD
    )
    assert user.is_platform_admin is False
    assert isinstance(user.id, UserId)
    assert isinstance(user.username, Username)
    assert isinstance(user.email, EmailAddress)
