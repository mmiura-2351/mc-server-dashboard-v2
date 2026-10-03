"""Unit tests for the UpdateProfile use case (self-service profile edit).

Drives the use case against in-memory fakes (TESTING.md Section 4). Verifies:
username and/or email are updated, omitted fields are left untouched, and the
registration-style uniqueness errors are raised on a conflict with another user.
A change to one's own current value is not a conflict (idempotent).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import replace

import pytest

from mc_server_dashboard_api.identity.application.update_profile import UpdateProfile
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import (
    EmailAlreadyExistsError,
    UsernameAlreadyExistsError,
    UserNotFoundError,
)
from mc_server_dashboard_api.identity.domain.value_objects import UserId, Username
from tests.identity.fakes import (
    FakeClock,
    FakeUnitOfWork,
    FakeUserRepository,
    make_user,
)

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)


def _use_case(uow: FakeUnitOfWork) -> UpdateProfile:
    return UpdateProfile(uow=uow, clock=FakeClock(_NOW))


async def test_update_username_and_email() -> None:
    user = make_user(username="alice", email="alice@example.com", now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)

    updated = await _use_case(uow)(
        user_id=user.id, username="alice2", email="alice2@example.com"
    )

    assert updated.username.value == "alice2"
    assert updated.email.value == "alice2@example.com"
    assert uow.users.by_id[user.id].username.value == "alice2"
    assert uow.commits == 1


async def test_update_only_username_leaves_email() -> None:
    user = make_user(username="alice", email="alice@example.com", now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)

    updated = await _use_case(uow)(user_id=user.id, username="alice2", email=None)

    assert updated.username.value == "alice2"
    assert updated.email.value == "alice@example.com"


async def test_update_no_fields_is_noop_without_commit() -> None:
    # An empty PATCH returns the current profile and does not commit, so
    # updated_at is not bumped on a no-change request.
    user = make_user(username="alice", email="alice@example.com", now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)

    updated = await _use_case(uow)(user_id=user.id, username=None, email=None)

    assert updated.username.value == "alice"
    assert updated.email.value == "alice@example.com"
    assert updated.updated_at == _NOW
    assert uow.commits == 0


async def test_update_username_conflict_raises() -> None:
    user = make_user(username="alice", email="alice@example.com", now=_NOW)
    other = make_user(username="bob", email="bob@example.com", now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    uow.users.seed(other)

    with pytest.raises(UsernameAlreadyExistsError):
        await _use_case(uow)(user_id=user.id, username="bob", email=None)
    assert uow.commits == 0


async def test_update_email_conflict_raises() -> None:
    user = make_user(username="alice", email="alice@example.com", now=_NOW)
    other = make_user(username="bob", email="bob@example.com", now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    uow.users.seed(other)

    with pytest.raises(EmailAlreadyExistsError):
        await _use_case(uow)(user_id=user.id, username=None, email="bob@example.com")
    assert uow.commits == 0


async def test_update_to_own_current_value_is_not_a_conflict() -> None:
    user = make_user(username="alice", email="alice@example.com", now=_NOW)
    uow = FakeUnitOfWork()
    uow.users.seed(user)

    # Re-submitting the same username/email must not collide with oneself.
    updated = await _use_case(uow)(
        user_id=user.id, username="alice", email="alice@example.com"
    )
    assert updated.username.value == "alice"
    assert uow.commits == 1


async def test_update_unknown_user_raises() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(UserNotFoundError):
        await _use_case(uow)(user_id=UserId.new(), username="x", email=None)


class _ChangedAfterRead(FakeUserRepository):
    """Applies ``change`` when the edit pre-checks the username.

    That check runs after the use case's read and before its write, so the change
    stands in for a request that committed in between (#3214).
    """

    def __init__(self, change: Callable[[dict[UserId, User]], None]) -> None:
        super().__init__()
        self._change = change

    async def get_by_username(self, username: Username) -> User | None:
        self._change(self.by_id)
        return await super().get_by_username(username)


async def test_update_does_not_restore_security_state_changed_after_the_read() -> None:
    user = make_user(
        username="alice", email="alice@example.com", now=_NOW, is_platform_admin=True
    )

    def administrator_acts(rows: dict[UserId, User]) -> None:
        rows[user.id] = replace(
            rows[user.id], active=False, is_platform_admin=False, password_hash="new"
        )

    uow = FakeUnitOfWork(users=_ChangedAfterRead(administrator_acts))
    uow.users.seed(user)

    updated = await _use_case(uow)(user_id=user.id, username="alice2", email=None)

    stored = uow.users.by_id[user.id]
    assert stored.username.value == "alice2"
    assert (stored.active, stored.is_platform_admin, stored.password_hash) == (
        False,
        False,
        "new",
    )
    # The response is the persisted row, not the stale read.
    assert updated == stored


async def test_update_of_user_deleted_after_the_read_raises() -> None:
    user = make_user(username="alice", email="alice@example.com", now=_NOW)

    def account_deleted(rows: dict[UserId, User]) -> None:
        del rows[user.id]

    uow = FakeUnitOfWork(users=_ChangedAfterRead(account_deleted))
    uow.users.seed(user)

    with pytest.raises(UserNotFoundError):
        await _use_case(uow)(user_id=user.id, username="alice2", email=None)
    assert uow.commits == 0
