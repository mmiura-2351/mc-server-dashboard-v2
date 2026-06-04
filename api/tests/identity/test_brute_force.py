"""Unit tests for the pure brute-force / lockout math (SECURITY.md Section 2)."""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.identity.domain.brute_force import (
    backoff_duration,
    is_locked,
)

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
_BASE = dt.timedelta(minutes=15)
_MAX = dt.timedelta(days=1)


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
