"""Unit tests for the RegisterUser use case against faked Ports.

Exercises policy enforcement, uniqueness pre-checks, hashing, and persistence
without a database; the duplicate-via-DB-constraint race path is covered by the
integration tests.
"""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.identity.application.register_user import RegisterUser
from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.entities import RefreshToken, User
from mc_server_dashboard_api.identity.domain.errors import (
    EmailAlreadyExistsError,
    PasswordPolicyError,
    UsernameAlreadyExistsError,
)
from mc_server_dashboard_api.identity.domain.password_hasher import PasswordHasher
from mc_server_dashboard_api.identity.domain.password_policy import PasswordPolicy
from mc_server_dashboard_api.identity.domain.repositories import (
    RefreshTokenRepository,
    UserRepository,
)
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    UserId,
    Username,
)

_VALID_PASSWORD = "Wm7!qz#Lp2vT"


class _FakeUserRepository(UserRepository):
    def __init__(self) -> None:
        self.added: list[User] = []
        self._by_username: dict[str, User] = {}
        self._by_email: dict[str, User] = {}

    def seed(self, user: User) -> None:
        self._by_username[user.username.value.casefold()] = user
        self._by_email[user.email.value] = user

    async def add(self, user: User) -> None:
        self.added.append(user)

    async def get_by_id(self, user_id: UserId) -> User | None:
        raise NotImplementedError

    async def get_by_username(self, username: Username) -> User | None:
        return self._by_username.get(username.value.casefold())

    async def get_by_email(self, email: EmailAddress) -> User | None:
        return self._by_email.get(email.value)

    async def usernames_by_id(self, user_ids: list[UserId]) -> dict[UserId, Username]:
        raise NotImplementedError

    async def update(self, user: User) -> None:
        raise NotImplementedError

    async def delete(self, user_id: UserId) -> None:
        raise NotImplementedError

    async def list_page(self, *, limit: int, offset: int) -> list[User]:
        raise NotImplementedError

    async def count_all(self) -> int:
        raise NotImplementedError

    async def count_active_platform_admins(self) -> int:
        raise NotImplementedError


class _FakeRefreshTokenRepository(RefreshTokenRepository):
    async def add(self, token: RefreshToken) -> None:
        raise NotImplementedError

    async def get_by_token_hash(self, token_hash: str) -> RefreshToken | None:
        raise NotImplementedError

    async def revoke(self, token_hash: str, *, revoked_at: dt.datetime) -> None:
        raise NotImplementedError

    async def revoke_all_for_user(
        self, user_id: UserId, *, revoked_at: dt.datetime
    ) -> None:
        raise NotImplementedError


class _FakeUnitOfWork(UnitOfWork):
    def __init__(self, users: _FakeUserRepository) -> None:
        self.users = users
        self.refresh_tokens = _FakeRefreshTokenRepository()
        self.committed = False

    async def __aenter__(self) -> "_FakeUnitOfWork":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None


class _FixedClock(Clock):
    def now(self) -> dt.datetime:
        return dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)


class _StubHasher(PasswordHasher):
    def hash(self, plaintext: str) -> str:
        return f"hashed::{plaintext}"

    def verify(self, plaintext: str, password_hash: str) -> bool:
        return password_hash == f"hashed::{plaintext}"


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


def _register(users: _FakeUserRepository) -> RegisterUser:
    return RegisterUser(
        uow=_FakeUnitOfWork(users),
        hasher=_StubHasher(),
        clock=_FixedClock(),
        policy=_policy(),
    )


async def test_registers_user_persists_hash_not_plaintext() -> None:
    users = _FakeUserRepository()
    uow = _FakeUnitOfWork(users)
    use_case = RegisterUser(
        uow=uow, hasher=_StubHasher(), clock=_FixedClock(), policy=_policy()
    )

    user = await use_case(
        username="alice", email="alice@example.com", password=_VALID_PASSWORD
    )

    assert user.username.value == "alice"
    assert user.email.value == "alice@example.com"
    assert user.password_hash == f"hashed::{_VALID_PASSWORD}"
    assert user.created_at == _FixedClock().now()
    assert uow.committed is True
    assert users.added == [user]


async def test_rejects_weak_password_before_persisting() -> None:
    users = _FakeUserRepository()
    with pytest.raises(PasswordPolicyError) as exc:
        await _register(users)(
            username="alice", email="alice@example.com", password="short"
        )
    assert exc.value.reason == "too_short"
    assert users.added == []


async def test_rejects_duplicate_username_precheck() -> None:
    users = _FakeUserRepository()
    users.seed(
        User(
            id=UserId.new(),
            username=Username("Alice"),
            email=EmailAddress("other@example.com"),
            password_hash="x",
            created_at=_FixedClock().now(),
            updated_at=_FixedClock().now(),
        )
    )
    with pytest.raises(UsernameAlreadyExistsError):
        await _register(users)(
            username="alice", email="alice@example.com", password=_VALID_PASSWORD
        )
    assert users.added == []


async def test_rejects_duplicate_email_precheck() -> None:
    users = _FakeUserRepository()
    users.seed(
        User(
            id=UserId.new(),
            username=Username("bob"),
            email=EmailAddress("alice@example.com"),
            password_hash="x",
            created_at=_FixedClock().now(),
            updated_at=_FixedClock().now(),
        )
    )
    with pytest.raises(EmailAlreadyExistsError):
        await _register(users)(
            username="alice", email="alice@example.com", password=_VALID_PASSWORD
        )
    assert users.added == []
