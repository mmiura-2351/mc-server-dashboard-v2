"""Unit tests for the pure brute-force / lockout math (SECURITY.md Section 2)."""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.identity.domain.brute_force import (
    backoff_cap_count,
    backoff_duration,
    is_locked,
    prune_horizon,
)
from mc_server_dashboard_api.identity.domain.registration import RegistrationConfig
from tests.identity.fakes import make_brute_force_config

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
_BASE = dt.timedelta(minutes=15)
_MAX = dt.timedelta(days=1)


def _registration(
    *, ip_limit_enabled: bool, ip_window: dt.timedelta
) -> RegistrationConfig:
    return RegistrationConfig(
        open=True,
        ip_limit_enabled=ip_limit_enabled,
        ip_threshold=5,
        ip_window=ip_window,
    )


def test_is_locked_none_is_never_locked() -> None:
    assert is_locked(None, now=_NOW) is False


def test_is_locked_future_until_is_locked() -> None:
    assert is_locked(_NOW + dt.timedelta(seconds=1), now=_NOW) is True


def test_is_locked_past_until_is_not_locked() -> None:
    assert is_locked(_NOW - dt.timedelta(seconds=1), now=_NOW) is False


def test_is_locked_exactly_now_is_not_locked() -> None:
    # locked_until == now means the lockout has just elapsed.
    assert is_locked(_NOW, now=_NOW) is False


def test_first_lockout_uses_base() -> None:
    assert backoff_duration(0, base=_BASE, maximum=_MAX) == _BASE


def test_backoff_doubles_per_prior_lockout() -> None:
    assert backoff_duration(1, base=_BASE, maximum=_MAX) == _BASE * 2
    assert backoff_duration(2, base=_BASE, maximum=_MAX) == _BASE * 4
    assert backoff_duration(3, base=_BASE, maximum=_MAX) == _BASE * 8


def test_backoff_capped_at_maximum() -> None:
    # 15min * 2**10 far exceeds one day; it is clamped to the cap.
    assert backoff_duration(10, base=_BASE, maximum=_MAX) == _MAX


def test_backoff_caps_at_timedelta_overflow_boundary() -> None:
    # base * 2**37 exceeds timedelta.max at the default base, so multiplying
    # before capping raises OverflowError (issue #1997).
    assert backoff_duration(37, base=_BASE, maximum=_MAX) == _MAX


def test_backoff_caps_at_absurd_lockout_count() -> None:
    # lockout_count is persisted and only reset by a successful login, so a
    # sustained attacker can grind it arbitrarily high; the cap must still hold
    # without computing a giant power.
    assert backoff_duration(1000, base=_BASE, maximum=_MAX) == _MAX


def test_backoff_saturates_from_the_cap_count() -> None:
    # backoff_cap_count is the single bound the login use case also clamps the
    # persisted lockout_count to (issue #2033), so the two cannot drift: at the
    # cap the back-off is already pinned to maximum, and the tier just below it
    # is still strictly shorter.
    cap = backoff_cap_count(base=_BASE, maximum=_MAX)
    assert backoff_duration(cap, base=_BASE, maximum=_MAX) == _MAX
    assert backoff_duration(cap - 1, base=_BASE, maximum=_MAX) < _MAX


def test_prune_horizon_is_longest_login_window() -> None:
    config = make_brute_force_config(
        username_window=dt.timedelta(minutes=40),
        ip_window=dt.timedelta(minutes=20),
    )
    assert prune_horizon(config) == dt.timedelta(minutes=40)


def test_prune_horizon_covers_enabled_registration_window() -> None:
    # The registration per-IP window (1h) outlives both login windows, so the
    # horizon must stretch to it — otherwise registration rows are pruned early
    # and the registration cap silently shrinks (issue #362).
    config = make_brute_force_config(
        username_window=dt.timedelta(minutes=15),
        ip_window=dt.timedelta(minutes=5),
    )
    registration = _registration(ip_limit_enabled=True, ip_window=dt.timedelta(hours=1))
    assert prune_horizon(config, registration) == dt.timedelta(hours=1)


def test_prune_horizon_ignores_disabled_registration_window() -> None:
    config = make_brute_force_config(
        username_window=dt.timedelta(minutes=15),
        ip_window=dt.timedelta(minutes=5),
    )
    registration = _registration(
        ip_limit_enabled=False, ip_window=dt.timedelta(hours=1)
    )
    assert prune_horizon(config, registration) == dt.timedelta(minutes=15)


def test_prune_horizon_keeps_longer_login_window_over_registration() -> None:
    config = make_brute_force_config(
        username_window=dt.timedelta(hours=2),
        ip_window=dt.timedelta(minutes=5),
    )
    registration = _registration(ip_limit_enabled=True, ip_window=dt.timedelta(hours=1))
    assert prune_horizon(config, registration) == dt.timedelta(hours=2)
