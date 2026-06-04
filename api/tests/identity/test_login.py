"""Unit tests for the Login use case against faked Ports.

Covers the success path (a pair is issued and the refresh row persisted), and
the uniform-failure posture: unknown-user and wrong-password are the same error,
both run the artificial-delay hook, and neither commits a token row.
"""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.identity.application.login import Login
from mc_server_dashboard_api.identity.domain.errors import InvalidCredentialsError
from tests.identity.fakes import (
    FakeClock,
    FakeTokenService,
    FakeUnitOfWork,
    RecordingFailureDelay,
    StubHasher,
    make_user,
)

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
_PASSWORD = "Wm7!qz#Lp2vT"
_REFRESH_TTL = dt.timedelta(days=14)


def _login(uow: FakeUnitOfWork, delay: RecordingFailureDelay) -> Login:
    return Login(
        uow=uow,
        hasher=StubHasher(),
        tokens=FakeTokenService(),
        clock=FakeClock(_NOW),
        failure_delay=delay,
        refresh_ttl=_REFRESH_TTL,
    )


async def test_login_success_issues_pair_and_persists_refresh() -> None:
    user = make_user(password=_PASSWORD)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    delay = RecordingFailureDelay()

    pair = await _login(uow, delay)(username="alice", password=_PASSWORD)

    assert pair.access_token == f"access::{user.id.value}"
    assert pair.refresh_token == "refresh-secret-1"
    stored = uow.refresh_tokens.by_hash["hash::refresh-secret-1"]
    assert stored.user_id == user.id
    assert stored.issued_at == _NOW
    assert stored.expires_at == _NOW + _REFRESH_TTL
    assert uow.commits == 1
    assert delay.calls == 0


async def test_login_wrong_password_is_uniform_failure() -> None:
    user = make_user(password=_PASSWORD)
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    delay = RecordingFailureDelay()

    with pytest.raises(InvalidCredentialsError):
        await _login(uow, delay)(username="alice", password="wrong-password")

    assert uow.refresh_tokens.by_hash == {}
    assert uow.commits == 0
    assert delay.calls == 1


async def test_login_unknown_user_is_same_error_as_wrong_password() -> None:
    uow = FakeUnitOfWork()  # no users seeded
    delay = RecordingFailureDelay()

    with pytest.raises(InvalidCredentialsError):
        await _login(uow, delay)(username="ghost", password=_PASSWORD)

    assert uow.refresh_tokens.by_hash == {}
    assert uow.commits == 0
    assert delay.calls == 1
