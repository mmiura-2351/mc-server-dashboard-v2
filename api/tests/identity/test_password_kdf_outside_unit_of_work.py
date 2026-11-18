"""The password KDF never runs while a unit of work is open (#3861).

An argon2 hash or verify takes about a second and 64 MiB. A unit of work holds a
pooled database connection from its first statement until it ends, and any row
or advisory lock it took, so a KDF call inside one ties both up for the whole
computation. Every identity use case that hashes or verifies a password is
driven here with a hasher that notes, for each call, whether a unit-of-work
block was open.
"""

from __future__ import annotations

import contextlib
import datetime as dt

import pytest

from mc_server_dashboard_api.identity.application.admin_create_user import (
    AdminCreateUser,
)
from mc_server_dashboard_api.identity.application.change_password import ChangePassword
from mc_server_dashboard_api.identity.application.delete_account import DeleteAccount
from mc_server_dashboard_api.identity.application.login import DummyPasswordHash, Login
from mc_server_dashboard_api.identity.application.register_user import RegisterUser
from mc_server_dashboard_api.identity.domain.errors import (
    InvalidCredentialsError,
    PasswordPolicyError,
)
from mc_server_dashboard_api.identity.domain.password_policy import PasswordPolicy
from tests.identity.fakes import (
    FakeClock,
    FakeLoginAttemptStore,
    FakeTokenService,
    FakeUnitOfWork,
    RecordingFailureDelay,
    StubHasher,
    make_brute_force_config,
    make_user,
)

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
_PASSWORD = "Wm7!qz#Lp2vT"
_NEW = "Np4@xZ#Lq9wR"


class _WatchingHasher(StubHasher):
    """Notes each call and whether ``uow`` had a block open when it was made."""

    def __init__(self, uow: FakeUnitOfWork) -> None:
        super().__init__()
        self._uow = uow
        self.calls: list[tuple[str, str]] = []

    def _where(self) -> str:
        return "inside" if self._uow.open_blocks else "outside"

    async def hash(self, plaintext: str) -> str:
        self.calls.append(("hash", self._where()))
        return await super().hash(plaintext)

    async def verify(self, plaintext: str, password_hash: str) -> bool:
        self.calls.append(("verify", self._where()))
        return await super().verify(plaintext, password_hash)


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
        common_passwords=frozenset(),
    )


@pytest.mark.parametrize(
    ("password", "refused"),
    [(_PASSWORD, False), ("not-the-password", True)],
    ids=["right", "wrong"],
)
async def test_delete_account_verifies_outside_the_unit_of_work(
    password: str, refused: bool
) -> None:
    user = make_user(password=_PASSWORD, now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    hasher = _WatchingHasher(uow)

    with (
        pytest.raises(InvalidCredentialsError) if refused else contextlib.nullcontext()
    ):
        await DeleteAccount(uow=uow, hasher=hasher, clock=FakeClock(_NOW))(
            user_id=user.id, password=password
        )

    assert hasher.calls == [("verify", "outside")]


async def test_change_password_verifies_and_hashes_outside_the_unit_of_work() -> None:
    user = make_user(password=_PASSWORD, now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    hasher = _WatchingHasher(uow)

    await ChangePassword(
        uow=uow, hasher=hasher, clock=FakeClock(_NOW), policy=_policy()
    )(user_id=user.id, current_password=_PASSWORD, new_password=_NEW)

    assert hasher.calls == [("verify", "outside"), ("hash", "outside")]


async def test_change_password_rejects_a_policy_violation_before_hashing() -> None:
    # The policy refuses the new password before the hasher runs: a rejected
    # password costs no KDF and answers 422, never the hasher's 503.
    user = make_user(password=_PASSWORD, now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    hasher = _WatchingHasher(uow)

    with pytest.raises(PasswordPolicyError):
        await ChangePassword(
            uow=uow, hasher=hasher, clock=FakeClock(_NOW), policy=_policy()
        )(user_id=user.id, current_password=_PASSWORD, new_password="short")

    assert hasher.calls == [("verify", "outside")]


@pytest.mark.parametrize(
    ("username", "password", "refused"),
    [
        ("alice", _PASSWORD, False),
        ("alice", "not-the-password", True),
        ("nobody", _PASSWORD, True),
    ],
    ids=["right-password", "wrong-password", "unknown-user"],
)
async def test_login_hashes_and_verifies_outside_the_unit_of_work(
    username: str, password: str, refused: bool
) -> None:
    # The same hasher builds the unknown-user dummy hash, so its first build is
    # watched too.
    uow = FakeUnitOfWork()
    uow.users.seed(make_user(username="alice", password=_PASSWORD))
    hasher = _WatchingHasher(uow)
    login = Login(
        uow=uow,
        attempts=FakeLoginAttemptStore(),
        brute_force=make_brute_force_config(),
        hasher=hasher,
        dummy_password_hash=DummyPasswordHash(hasher, "__dummy__"),
        tokens=FakeTokenService(),
        clock=FakeClock(_NOW),
        failure_delay=RecordingFailureDelay(),
        refresh_ttl=dt.timedelta(days=14),
    )

    with (
        pytest.raises(InvalidCredentialsError) if refused else contextlib.nullcontext()
    ):
        await login(username=username, password=password)

    assert hasher.calls == [("hash", "outside"), ("verify", "outside")]


async def test_register_user_hashes_outside_the_unit_of_work() -> None:
    uow = FakeUnitOfWork()
    hasher = _WatchingHasher(uow)

    await RegisterUser(uow=uow, hasher=hasher, clock=FakeClock(_NOW), policy=_policy())(
        username="alice", email="alice@example.com", password=_PASSWORD
    )

    assert hasher.calls == [("hash", "outside")]


async def test_admin_create_user_hashes_outside_the_unit_of_work() -> None:
    uow = FakeUnitOfWork()
    hasher = _WatchingHasher(uow)

    await AdminCreateUser(
        uow=uow, hasher=hasher, clock=FakeClock(_NOW), policy=_policy()
    )(username="alice", email="alice@example.com", password=_PASSWORD)

    assert hasher.calls == [("hash", "outside")]
