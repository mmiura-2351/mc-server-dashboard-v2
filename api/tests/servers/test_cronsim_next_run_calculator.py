"""Unit tests for the cronsim-backed ``NextRunCalculator`` adapter (issue #1835).

Pure computation (no I/O), so it unit-tests directly: 5-field validation, the
strictly-after contract, timezone-local evaluation, and the DST transitions the
acceptance criteria pin — a nonexistent local time (spring forward) fires right
after the gap, Debian-cron style, and a repeated local time (fall back) fires
once, not twice.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from mc_server_dashboard_api.servers.adapters.cronsim_next_run_calculator import (
    CronsimNextRunCalculator,
)
from mc_server_dashboard_api.servers.domain.errors import InvalidCronExpressionError
from mc_server_dashboard_api.servers.domain.next_run_calculator import (
    NextRunCalculator,
)

_UTC = dt.timezone.utc


@pytest.fixture
def calculator() -> NextRunCalculator:
    return CronsimNextRunCalculator()


def test_validate_accepts_five_field_expression(
    calculator: NextRunCalculator,
) -> None:
    calculator.validate("*/5 * * * *")


@pytest.mark.parametrize("bad", ["61 * * * *", "* * *", "not a cron", ""])
def test_validate_rejects_malformed_expression(
    calculator: NextRunCalculator, bad: str
) -> None:
    with pytest.raises(InvalidCronExpressionError):
        calculator.validate(bad)


def test_next_after_returns_first_utc_occurrence(
    calculator: NextRunCalculator,
) -> None:
    after = dt.datetime(2026, 7, 1, 2, 59, tzinfo=_UTC)
    occurrence = calculator.next_after("0 3 * * *", "UTC", after)
    assert occurrence == dt.datetime(2026, 7, 1, 3, 0, tzinfo=_UTC)
    assert occurrence.tzinfo == _UTC


def test_next_after_is_strictly_after(calculator: NextRunCalculator) -> None:
    exactly_at = dt.datetime(2026, 7, 1, 3, 0, tzinfo=_UTC)
    occurrence = calculator.next_after("0 3 * * *", "UTC", exactly_at)
    assert occurrence == dt.datetime(2026, 7, 2, 3, 0, tzinfo=_UTC)


def test_next_after_evaluates_in_the_schedule_timezone(
    calculator: NextRunCalculator,
) -> None:
    # 03:00 in Tokyo (UTC+9, no DST) is 18:00 UTC the previous day.
    after = dt.datetime(2026, 7, 1, 0, 0, tzinfo=_UTC)
    occurrence = calculator.next_after("0 3 * * *", "Asia/Tokyo", after)
    assert occurrence == dt.datetime(2026, 7, 1, 18, 0, tzinfo=_UTC)


def test_next_after_accepts_after_in_any_timezone(
    calculator: NextRunCalculator,
) -> None:
    # The same instant expressed in another zone yields the same occurrence.
    after_utc = dt.datetime(2026, 7, 1, 2, 59, tzinfo=_UTC)
    after_tokyo = after_utc.astimezone(ZoneInfo("Asia/Tokyo"))
    assert calculator.next_after("0 3 * * *", "UTC", after_tokyo) == (
        calculator.next_after("0 3 * * *", "UTC", after_utc)
    )


def test_spring_forward_gap_fires_right_after_the_jump(
    calculator: NextRunCalculator,
) -> None:
    # Europe/Berlin 2026-03-29: 02:00 CET jumps to 03:00 CEST, so 02:30 does not
    # exist. Debian-cron semantics (cronsim): the job fires at the moment after
    # the gap — 03:00 CEST, i.e. 01:00 UTC.
    after = dt.datetime(2026, 3, 29, 0, 0, tzinfo=_UTC)
    occurrence = calculator.next_after("30 2 * * *", "Europe/Berlin", after)
    assert occurrence == dt.datetime(2026, 3, 29, 1, 0, tzinfo=_UTC)
    # The following day is back to a plain 02:30 CEST (00:30 UTC).
    following = calculator.next_after("30 2 * * *", "Europe/Berlin", occurrence)
    assert following == dt.datetime(2026, 3, 30, 0, 30, tzinfo=_UTC)


def test_fall_back_repeated_hour_fires_once(calculator: NextRunCalculator) -> None:
    # Europe/Berlin 2026-10-25: 03:00 CEST falls back to 02:00 CET, so 02:30
    # occurs twice. The job fires on the first (CEST, 00:30 UTC) pass only; the
    # next occurrence is the following day, not the repeated hour (01:30 UTC).
    after = dt.datetime(2026, 10, 24, 23, 0, tzinfo=_UTC)
    first = calculator.next_after("30 2 * * *", "Europe/Berlin", after)
    assert first == dt.datetime(2026, 10, 25, 0, 30, tzinfo=_UTC)
    second = calculator.next_after("30 2 * * *", "Europe/Berlin", first)
    assert second == dt.datetime(2026, 10, 26, 1, 30, tzinfo=_UTC)
