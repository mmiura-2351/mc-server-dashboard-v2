"""Unit tests for the periodic login_attempt prune use case (SECURITY.md 3).

Drives :class:`PruneLoginAttempts.tick` against the in-memory fakes (a faked
:class:`Clock` and the fake :class:`LoginAttemptStore`), asserting one tick drops
rows past the longest sliding window and keeps in-window rows — independent of any
login event.
"""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.identity.application.prune_login_attempts import (
    PruneLoginAttempts,
)
from mc_server_dashboard_api.identity.domain.registration import RegistrationConfig
from tests.identity.fakes import (
    FakeClock,
    FakeLoginAttemptStore,
    make_brute_force_config,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
# Longest window of the default config: max(15min username, 5min ip) == 15min.
_USERNAME_WINDOW = dt.timedelta(minutes=15)


async def _record(
    store: FakeLoginAttemptStore, *, at: dt.datetime, success: bool = False
) -> None:
    await store.record_attempt(
        username="alice", ip="10.0.0.1", success=success, failure_reason=None, at=at
    )


async def test_tick_deletes_rows_older_than_longest_window() -> None:
    store = FakeLoginAttemptStore()
    # Old: outside the 15-minute window. In-window: just inside it.
    await _record(store, at=_NOW - _USERNAME_WINDOW - dt.timedelta(seconds=1))
    await _record(store, at=_NOW - dt.timedelta(minutes=1))

    pruner = PruneLoginAttempts(
        attempts=store,
        brute_force=make_brute_force_config(),
        clock=FakeClock(_NOW),
    )
    await pruner.tick()

    remaining_times = [a[4] for a in store.attempts]
    assert remaining_times == [_NOW - dt.timedelta(minutes=1)]


async def test_tick_keeps_all_in_window_rows() -> None:
    store = FakeLoginAttemptStore()
    await _record(store, at=_NOW)
    await _record(store, at=_NOW - dt.timedelta(minutes=14))

    pruner = PruneLoginAttempts(
        attempts=store,
        brute_force=make_brute_force_config(),
        clock=FakeClock(_NOW),
    )
    await pruner.tick()

    assert len(store.attempts) == 2


async def test_tick_uses_the_longest_of_the_two_windows() -> None:
    store = FakeLoginAttemptStore()
    # 30min ago: outside the 20-minute ip window but inside the 40-minute
    # username window — the longest bound must keep it.
    await _record(store, at=_NOW - dt.timedelta(minutes=30))

    pruner = PruneLoginAttempts(
        attempts=store,
        brute_force=make_brute_force_config(
            username_window=dt.timedelta(minutes=40),
            ip_window=dt.timedelta(minutes=20),
        ),
        clock=FakeClock(_NOW),
    )
    await pruner.tick()

    assert len(store.attempts) == 1


async def test_tick_keeps_registration_rows_within_their_window() -> None:
    # Registration rows live in the same table but are counted over the wider
    # registration per-IP window (1h here). With only the 15-minute login window
    # the prune would delete a 30-minute-old registration row and silently shrink
    # the registration cap (issue #362) — passing the registration config widens
    # the horizon to keep it.
    store = FakeLoginAttemptStore()
    await store.record_registration(ip="10.0.0.1", at=_NOW - dt.timedelta(minutes=30))

    pruner = PruneLoginAttempts(
        attempts=store,
        brute_force=make_brute_force_config(),
        clock=FakeClock(_NOW),
        registration=RegistrationConfig(
            open=True,
            ip_limit_enabled=True,
            ip_threshold=5,
            ip_window=dt.timedelta(hours=1),
        ),
    )
    await pruner.tick()

    assert len(store.attempts) == 1
