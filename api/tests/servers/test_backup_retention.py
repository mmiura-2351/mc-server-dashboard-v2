"""Domain tests for the backup retention policy (issue #1841).

Pins the :class:`RetentionPolicy` value object (keep-N XOR tiered, validated on
construction and on the JSON round-trip) and the pure selection function
:func:`backups_to_prune`: only ``source=scheduled`` rows are ever candidates,
keep-N keeps the newest N, and the tiered form keeps the newest backup per UTC
calendar day / ISO week / calendar month over the configured windows.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.backup_retention import (
    RetentionPolicy,
    backups_to_prune,
)
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidRetentionPolicyError,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId

_NOW = dt.datetime(2026, 7, 10, 12, 0, tzinfo=dt.timezone.utc)
_SERVER = ServerId(uuid.uuid4())


def _backup(
    created_at: dt.datetime,
    *,
    source: BackupSource = BackupSource.SCHEDULED,
    backup_id: BackupId | None = None,
) -> Backup:
    return Backup(
        id=backup_id or BackupId.new(),
        server_id=_SERVER,
        storage_ref=f"ref-{uuid.uuid4()}",
        size_bytes=None,
        source=source,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=created_at,
    )


# --- policy validation -------------------------------------------------------


def test_keep_last_policy_is_valid() -> None:
    policy = RetentionPolicy.from_fields(keep_last=3)
    assert policy.keep_last == 3
    assert (policy.daily, policy.weekly, policy.monthly) == (0, 0, 0)


def test_tiered_policy_is_valid() -> None:
    policy = RetentionPolicy.from_fields(daily=7, weekly=4, monthly=6)
    assert policy.keep_last is None
    assert (policy.daily, policy.weekly, policy.monthly) == (7, 4, 6)


def test_tiered_policy_omitted_tiers_default_to_zero() -> None:
    policy = RetentionPolicy.from_fields(daily=7)
    assert (policy.daily, policy.weekly, policy.monthly) == (7, 0, 0)


def test_keep_last_must_be_at_least_one() -> None:
    with pytest.raises(InvalidRetentionPolicyError):
        RetentionPolicy.from_fields(keep_last=0)


def test_keep_last_rejects_bool() -> None:
    with pytest.raises(InvalidRetentionPolicyError):
        RetentionPolicy.from_fields(keep_last=True)


def test_negative_tier_is_rejected() -> None:
    with pytest.raises(InvalidRetentionPolicyError):
        RetentionPolicy.from_fields(daily=-1, weekly=1)


def test_all_zero_tiers_are_rejected() -> None:
    with pytest.raises(InvalidRetentionPolicyError):
        RetentionPolicy.from_fields(daily=0, weekly=0, monthly=0)


def test_keep_last_and_tiers_together_are_rejected() -> None:
    with pytest.raises(InvalidRetentionPolicyError):
        RetentionPolicy.from_fields(keep_last=3, daily=7)


def test_empty_policy_is_rejected() -> None:
    with pytest.raises(InvalidRetentionPolicyError):
        RetentionPolicy.from_fields()


# --- JSON round-trip ---------------------------------------------------------


def test_keep_last_json_round_trip() -> None:
    policy = RetentionPolicy.from_fields(keep_last=5)
    assert policy.to_json() == {"keep_last": 5}
    assert RetentionPolicy.from_json(policy.to_json()) == policy


def test_tiered_json_round_trip() -> None:
    policy = RetentionPolicy.from_fields(daily=7, weekly=4, monthly=6)
    assert policy.to_json() == {"daily": 7, "weekly": 4, "monthly": 6}
    assert RetentionPolicy.from_json(policy.to_json()) == policy


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "keep",
        {},
        {"keep_last": "3"},
        {"keep_last": True},
        {"keep_last": 0},
        {"keep_last": 3, "daily": 1},
        {"daily": 1},  # the stored tiered form carries all three tiers
        {"daily": 1, "weekly": 0, "monthly": 0, "extra": 1},
        {"daily": 0, "weekly": 0, "monthly": 0},
        {"daily": -1, "weekly": 0, "monthly": 1},
        {"daily": 1.5, "weekly": 0, "monthly": 0},
    ],
)
def test_from_json_rejects_malformed_shapes(data: object) -> None:
    with pytest.raises(InvalidRetentionPolicyError):
        RetentionPolicy.from_json(data)


# --- keep-N selection --------------------------------------------------------


def test_keep_n_with_fewer_backups_prunes_nothing() -> None:
    policy = RetentionPolicy.from_fields(keep_last=3)
    backups = [
        _backup(_NOW - dt.timedelta(days=i)) for i in range(3)
    ]  # exactly 3 scheduled
    assert backups_to_prune(policy, backups, _NOW) == []


def test_keep_n_prunes_the_oldest_scheduled() -> None:
    policy = RetentionPolicy.from_fields(keep_last=3)
    oldest = _backup(_NOW - dt.timedelta(days=3))
    newer = [_backup(_NOW - dt.timedelta(days=i)) for i in range(3)]
    pruned = backups_to_prune(policy, [oldest, *newer], _NOW)
    assert pruned == [oldest]


def test_keep_n_returns_pruned_rows_newest_first() -> None:
    policy = RetentionPolicy.from_fields(keep_last=1)
    backups = [_backup(_NOW - dt.timedelta(days=i)) for i in range(4)]
    pruned = backups_to_prune(policy, backups, _NOW)
    # Everything but the newest, returned newest-first (the input listing order).
    assert pruned == backups[1:]


def test_keep_n_never_touches_manual_uploaded_or_event_rows() -> None:
    policy = RetentionPolicy.from_fields(keep_last=1)
    manual = _backup(_NOW - dt.timedelta(days=9), source=BackupSource.MANUAL)
    uploaded = _backup(_NOW - dt.timedelta(days=8), source=BackupSource.UPLOADED)
    event = _backup(_NOW - dt.timedelta(days=7), source=BackupSource.EVENT)
    scheduled_old = _backup(_NOW - dt.timedelta(days=1))
    scheduled_new = _backup(_NOW)
    pruned = backups_to_prune(
        policy, [manual, uploaded, event, scheduled_old, scheduled_new], _NOW
    )
    assert pruned == [scheduled_old]


def test_keep_n_tie_on_created_at_is_deterministic() -> None:
    policy = RetentionPolicy.from_fields(keep_last=1)
    when = _NOW - dt.timedelta(days=1)
    low = _backup(when, backup_id=BackupId(uuid.UUID(int=1)))
    high = _backup(when, backup_id=BackupId(uuid.UUID(int=2)))
    # Same instant: the tie breaks on the id, so the same row survives regardless
    # of input order.
    assert backups_to_prune(policy, [low, high], _NOW) == [low]
    assert backups_to_prune(policy, [high, low], _NOW) == [low]


# --- tiered selection --------------------------------------------------------


def test_daily_keeps_newest_per_day_within_window() -> None:
    policy = RetentionPolicy.from_fields(daily=2)
    today_morning = _backup(_NOW.replace(hour=6))
    today_noon = _backup(_NOW)  # newest today: kept
    yesterday = _backup(_NOW - dt.timedelta(days=1))  # newest yesterday: kept
    two_days_ago = _backup(_NOW - dt.timedelta(days=2))  # outside the window
    pruned = backups_to_prune(
        policy, [today_morning, today_noon, yesterday, two_days_ago], _NOW
    )
    assert set(p.id.value for p in pruned) == {
        today_morning.id.value,
        two_days_ago.id.value,
    }


def test_daily_window_uses_utc_day_boundaries() -> None:
    policy = RetentionPolicy.from_fields(daily=1)
    # 2026-07-09 23:30 UTC is yesterday in UTC even though it is 2026-07-10 in
    # UTC+9; only today's (UTC) backup is kept.
    late_yesterday_utc = _backup(
        dt.datetime(2026, 7, 9, 23, 30, tzinfo=dt.timezone.utc)
    )
    today = _backup(_NOW.replace(hour=1))
    pruned = backups_to_prune(policy, [late_yesterday_utc, today], _NOW)
    assert pruned == [late_yesterday_utc]


def test_weekly_keeps_newest_per_iso_week() -> None:
    policy = RetentionPolicy.from_fields(weekly=2)
    # _NOW (2026-07-10) is a Friday in ISO week 28; 2026-07-06 is the Monday of
    # the same week; 2026-07-05 is the Sunday closing week 27.
    this_week_late = _backup(_NOW)
    this_week_early = _backup(dt.datetime(2026, 7, 6, 8, 0, tzinfo=dt.timezone.utc))
    last_week = _backup(dt.datetime(2026, 7, 5, 8, 0, tzinfo=dt.timezone.utc))
    two_weeks_ago = _backup(dt.datetime(2026, 6, 28, 8, 0, tzinfo=dt.timezone.utc))
    pruned = backups_to_prune(
        policy, [this_week_late, this_week_early, last_week, two_weeks_ago], _NOW
    )
    assert set(p.id.value for p in pruned) == {
        this_week_early.id.value,
        two_weeks_ago.id.value,
    }


def test_monthly_keeps_newest_per_calendar_month() -> None:
    policy = RetentionPolicy.from_fields(monthly=2)
    this_month_late = _backup(_NOW)
    this_month_early = _backup(dt.datetime(2026, 7, 1, 0, 0, tzinfo=dt.timezone.utc))
    last_month = _backup(dt.datetime(2026, 6, 30, 23, 0, tzinfo=dt.timezone.utc))
    two_months_ago = _backup(dt.datetime(2026, 5, 31, 12, 0, tzinfo=dt.timezone.utc))
    pruned = backups_to_prune(
        policy, [this_month_late, this_month_early, last_month, two_months_ago], _NOW
    )
    assert set(p.id.value for p in pruned) == {
        this_month_early.id.value,
        two_months_ago.id.value,
    }


def test_monthly_window_wraps_the_year_boundary() -> None:
    policy = RetentionPolicy.from_fields(monthly=8)
    january = dt.datetime(2026, 1, 15, 12, 0, tzinfo=dt.timezone.utc)
    december = dt.datetime(2025, 12, 15, 12, 0, tzinfo=dt.timezone.utc)
    kept_jan = _backup(january)
    kept_dec = _backup(december)  # 8-month window from July reaches December
    pruned = backups_to_prune(policy, [kept_jan, kept_dec], _NOW)
    assert pruned == []


def test_backup_kept_by_any_tier_survives() -> None:
    # daily=1 alone would prune yesterday's backup, but weekly=1 keeps it as the
    # newest of the current ISO week (2026-07-09 and 2026-07-10 share week 28).
    policy = RetentionPolicy.from_fields(daily=1, weekly=1)
    yesterday = _backup(dt.datetime(2026, 7, 9, 23, 0, tzinfo=dt.timezone.utc))
    pruned = backups_to_prune(policy, [yesterday], _NOW)
    assert pruned == []


def test_tiered_prunes_scheduled_outside_all_buckets() -> None:
    policy = RetentionPolicy.from_fields(daily=1, weekly=1, monthly=1)
    ancient = _backup(dt.datetime(2025, 1, 1, 0, 0, tzinfo=dt.timezone.utc))
    current = _backup(_NOW)
    pruned = backups_to_prune(policy, [ancient, current], _NOW)
    assert pruned == [ancient]


def test_tiered_never_touches_manual_rows() -> None:
    policy = RetentionPolicy.from_fields(daily=1)
    ancient_manual = _backup(
        dt.datetime(2025, 1, 1, 0, 0, tzinfo=dt.timezone.utc),
        source=BackupSource.MANUAL,
    )
    pruned = backups_to_prune(policy, [ancient_manual], _NOW)
    assert pruned == []


def test_tiered_same_backup_claims_multiple_buckets() -> None:
    # A single backup that is the newest of its day, week, and month claims all
    # three buckets; an older sibling in the same buckets is pruned.
    policy = RetentionPolicy.from_fields(daily=1, weekly=1, monthly=1)
    newest = _backup(_NOW)
    older_same_day = _backup(_NOW - dt.timedelta(hours=2))
    pruned = backups_to_prune(policy, [newest, older_same_day], _NOW)
    assert pruned == [older_same_day]
