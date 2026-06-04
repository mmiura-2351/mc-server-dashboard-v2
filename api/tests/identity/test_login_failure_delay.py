"""Tests for the real artificial-delay adapter (SECURITY.md Section 2 step 5).

Proves the :class:`FixedLoginFailureDelay` awaits the configured duration through
the :class:`Sleeper` Port — and that the failure paths of the Login use case
invoke it — without ever really sleeping (a recording fake stands in for the
sleeper, TESTING.md Section 4).
"""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.identity.adapters.login_failure_delay import (
    FixedLoginFailureDelay,
)
from mc_server_dashboard_api.identity.application.login import Login
from mc_server_dashboard_api.identity.domain.errors import InvalidCredentialsError
from tests.identity.fakes import (
    FakeClock,
    FakeLoginAttemptStore,
    FakeTokenService,
    FakeUnitOfWork,
    RecordingSleeper,
    StubHasher,
    make_brute_force_config,
    make_user,
)

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
_PASSWORD = "Wm7!qz#Lp2vT"
_DELAY = dt.timedelta(milliseconds=200)


async def test_adapter_sleeps_configured_duration() -> None:
    sleeper = RecordingSleeper()
    await FixedLoginFailureDelay(delay=_DELAY, sleeper=sleeper).apply()
    assert sleeper.sleeps == [_DELAY]


async def test_login_failure_invokes_real_delay_without_sleeping() -> None:
    uow = FakeUnitOfWork()
    uow.users.seed(make_user(password=_PASSWORD))
    sleeper = RecordingSleeper()
    login = Login(
        uow=uow,
        attempts=FakeLoginAttemptStore(),
        brute_force=make_brute_force_config(delay=_DELAY),
        hasher=StubHasher(),
        tokens=FakeTokenService(),
        clock=FakeClock(_NOW),
        failure_delay=FixedLoginFailureDelay(delay=_DELAY, sleeper=sleeper),
        refresh_ttl=dt.timedelta(days=14),
    )

    with pytest.raises(InvalidCredentialsError):
        await login(username="alice", password="wrong", ip="198.51.100.1")

    assert sleeper.sleeps == [_DELAY]


async def test_login_success_does_not_delay() -> None:
    uow = FakeUnitOfWork()
    uow.users.seed(make_user(password=_PASSWORD))
    sleeper = RecordingSleeper()
    login = Login(
        uow=uow,
        attempts=FakeLoginAttemptStore(),
        brute_force=make_brute_force_config(delay=_DELAY),
        hasher=StubHasher(),
        tokens=FakeTokenService(),
        clock=FakeClock(_NOW),
        failure_delay=FixedLoginFailureDelay(delay=_DELAY, sleeper=sleeper),
        refresh_ttl=dt.timedelta(days=14),
    )

    await login(username="alice", password=_PASSWORD, ip="198.51.100.1")

    assert sleeper.sleeps == []
