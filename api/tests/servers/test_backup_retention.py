"""Domain tests for the backup retention policy (issue #1841).

Pins the :class:`RetentionPolicy` value object (keep-N XOR tiered, validated on
construction and on the JSON round-trip) and the pure selection function
:func:`backups_to_prune`: only ``source=scheduled`` rows are ever candidates,
keep-N keeps the newest N, and the tiered form keeps the newest backup per UTC
calendar day / ISO week / calendar month over the configured windows. Healthy
rows are chosen as keepers first (issue #1864): a quarantined row keeps only a
slot or bucket no sized non-quarantined row is left to fill. A row with no
recorded size, whose archive may be missing, ranks behind every sized row, a
quarantined one included (issue #4105).
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
    health: BackupHealth = BackupHealth.HEALTHY,
    size_bytes: int | None = 1024,
    backup_id: BackupId | None = None,
) -> Backup:
    return Backup(
        id=backup_id or BackupId.new(),
        server_id=_SERVER,
        storage_ref=f"ref-{uuid.uuid4()}",
        size_bytes=size_bytes,
        source=source,
        health=health,
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


# --- health-aware selection (issue #1864) ------------------------------------
#
# Healthy rows are chosen as keepers first: a QUARANTINED row keeps a keep-N
# slot only when too few other rows are left to fill the N, and a tier bucket
# only when the bucket holds no other row. UNKNOWN (a legacy row the sweep has
# not read yet) and UNREADABLE (a revisable verdict) rank like HEALTHY. Every
# row here has a recorded size; NULL-size rows have their own section below.

# One slot or one bucket each, so a single sibling can hold it.
_SINGLE_SLOT_POLICIES = {
    "keep_last": RetentionPolicy.from_fields(keep_last=1),
    "daily": RetentionPolicy.from_fields(daily=1),
    "weekly": RetentionPolicy.from_fields(weekly=1),
    "monthly": RetentionPolicy.from_fields(monthly=1),
}

# Where the candidate sits when it is not the newest row: older than the
# sibling for keep-N, which ranks rows rather than dating them, and a day / ISO
# week / month outside the one-wide window for the tiers.
_OLDER = {
    "keep_last": _NOW - dt.timedelta(hours=4),
    "daily": dt.datetime(2026, 7, 9, 12, 0, tzinfo=dt.timezone.utc),  # yesterday
    "weekly": dt.datetime(2026, 7, 5, 12, 0, tzinfo=dt.timezone.utc),  # ISO week 27
    "monthly": dt.datetime(2026, 6, 30, 12, 0, tzinfo=dt.timezone.utc),  # June
}

# health x age x tier -> which of the two rows is pruned. The candidate is the
# row under test; the sibling sits in the policy's one slot or bucket two hours
# before _NOW. Keep-N and the tiers part only where an older candidate meets a
# quarantined sibling: keep-N ranks a non-quarantined candidate ahead of it,
# while a row outside a tier window is never kept.
_TRUTH_TABLE = [
    # candidate, sibling, candidate newest?, pruned under keep-N, under a tier
    ("healthy", "healthy", True, "sibling", "sibling"),
    ("unknown", "healthy", True, "sibling", "sibling"),
    ("unreadable", "healthy", True, "sibling", "sibling"),
    ("quarantined", "healthy", True, "candidate", "candidate"),
    ("healthy", "quarantined", True, "sibling", "sibling"),
    ("unknown", "quarantined", True, "sibling", "sibling"),
    ("unreadable", "quarantined", True, "sibling", "sibling"),
    ("quarantined", "quarantined", True, "sibling", "sibling"),
    ("healthy", "healthy", False, "candidate", "candidate"),
    ("unknown", "healthy", False, "candidate", "candidate"),
    ("unreadable", "healthy", False, "candidate", "candidate"),
    ("quarantined", "healthy", False, "candidate", "candidate"),
    ("healthy", "quarantined", False, "sibling", "candidate"),
    ("unknown", "quarantined", False, "sibling", "candidate"),
    ("unreadable", "quarantined", False, "sibling", "candidate"),
    ("quarantined", "quarantined", False, "candidate", "candidate"),
]


@pytest.mark.parametrize("tier", list(_SINGLE_SLOT_POLICIES))
@pytest.mark.parametrize(
    ("candidate", "sibling", "newest", "pruned_keep_n", "pruned_tiered"),
    _TRUTH_TABLE,
)
def test_health_age_tier_truth_table(
    tier: str,
    candidate: str,
    sibling: str,
    newest: bool,
    pruned_keep_n: str,
    pruned_tiered: str,
) -> None:
    rows = {
        "candidate": _backup(
            _NOW if newest else _OLDER[tier], health=BackupHealth(candidate)
        ),
        "sibling": _backup(_NOW - dt.timedelta(hours=2), health=BackupHealth(sibling)),
    }
    pruned_row = pruned_keep_n if tier == "keep_last" else pruned_tiered
    pruned = backups_to_prune(_SINGLE_SLOT_POLICIES[tier], list(rows.values()), _NOW)
    assert pruned == [rows[pruned_row]]


@pytest.mark.parametrize("tier", list(_SINGLE_SLOT_POLICIES))
@pytest.mark.parametrize("health", [BackupHealth.UNKNOWN, BackupHealth.UNREADABLE])
def test_unknown_or_unreadable_row_takes_the_slot_a_quarantined_row_yields(
    tier: str, health: BackupHealth
) -> None:
    # Neither verdict is final, so the row ranks like a healthy one: it takes
    # the slot ahead of the quarantined newest row and of the healthy oldest.
    quarantined = _backup(_NOW, health=BackupHealth.QUARANTINED)
    unverified = _backup(_NOW - dt.timedelta(hours=1), health=health)
    healthy = _backup(_NOW - dt.timedelta(hours=2))
    pruned = backups_to_prune(
        _SINGLE_SLOT_POLICIES[tier], [quarantined, unverified, healthy], _NOW
    )
    assert pruned == [quarantined, healthy]


@pytest.mark.parametrize("tier", list(_SINGLE_SLOT_POLICIES))
def test_mixed_health_pruned_rows_stay_newest_first(tier: str) -> None:
    quarantined = _backup(_NOW, health=BackupHealth.QUARANTINED)
    kept = _backup(_NOW - dt.timedelta(hours=1))
    healthy_older = _backup(_NOW - dt.timedelta(hours=2))
    unreadable = _backup(_NOW - dt.timedelta(hours=3), health=BackupHealth.UNREADABLE)
    pruned = backups_to_prune(
        _SINGLE_SLOT_POLICIES[tier],
        [unreadable, healthy_older, kept, quarantined],
        _NOW,
    )
    assert pruned == [quarantined, healthy_older, unreadable]


def test_keep_n_fills_the_slots_healthy_rows_leave_with_the_newest_quarantined() -> (
    None
):
    # Two healthy rows for three slots: the newest quarantined row takes the
    # third, and the older quarantined rows go.
    policy = RetentionPolicy.from_fields(keep_last=3)
    quarantined_newest = _backup(_NOW, health=BackupHealth.QUARANTINED)
    healthy_1 = _backup(_NOW - dt.timedelta(days=1))
    quarantined_2 = _backup(
        _NOW - dt.timedelta(days=2), health=BackupHealth.QUARANTINED
    )
    healthy_3 = _backup(_NOW - dt.timedelta(days=3))
    quarantined_4 = _backup(
        _NOW - dt.timedelta(days=4), health=BackupHealth.QUARANTINED
    )
    pruned = backups_to_prune(
        policy,
        [quarantined_newest, healthy_1, quarantined_2, healthy_3, quarantined_4],
        _NOW,
    )
    assert pruned == [quarantined_2, quarantined_4]


def test_tiered_bucket_without_a_healthy_row_keeps_its_newest_quarantined() -> None:
    # Today holds only quarantined rows, so its newest one keeps the day;
    # yesterday's healthy row displaces the newer quarantined row beside it.
    policy = RetentionPolicy.from_fields(daily=2)
    today_newest = _backup(_NOW, health=BackupHealth.QUARANTINED)
    today_older = _backup(_NOW - dt.timedelta(hours=2), health=BackupHealth.QUARANTINED)
    yesterday_quarantined = _backup(
        dt.datetime(2026, 7, 9, 12, 0, tzinfo=dt.timezone.utc),
        health=BackupHealth.QUARANTINED,
    )
    yesterday_healthy = _backup(dt.datetime(2026, 7, 9, 8, 0, tzinfo=dt.timezone.utc))
    pruned = backups_to_prune(
        policy,
        [today_newest, today_older, yesterday_quarantined, yesterday_healthy],
        _NOW,
    )
    assert pruned == [today_older, yesterday_quarantined]


def test_quarantined_newest_yields_the_week_and_still_keeps_its_day() -> None:
    # Yesterday's healthy row takes the current ISO week ahead of today's
    # quarantined row, which still keeps today: no other row is in that day.
    policy = RetentionPolicy.from_fields(daily=1, weekly=1)
    quarantined_today = _backup(_NOW, health=BackupHealth.QUARANTINED)
    yesterday = _backup(dt.datetime(2026, 7, 9, 12, 0, tzinfo=dt.timezone.utc))
    pruned = backups_to_prune(policy, [quarantined_today, yesterday], _NOW)
    assert pruned == []


def test_one_healthy_row_takes_two_buckets_from_two_quarantined_rows() -> None:
    # The accepted case where overlapping tiers keep fewer rows than ignoring
    # health would: ISO week 27 spans June and July, and the healthy June 29 row
    # takes both week 27 and June, so neither quarantined row keeps a bucket.
    # Health-blind, Y would keep week 27 and X June, and Z would go.
    policy = RetentionPolicy.from_fields(weekly=2, monthly=2)
    w = _backup(_NOW)  # week 28, July
    y = _backup(
        dt.datetime(2026, 7, 3, 12, 0, tzinfo=dt.timezone.utc),  # week 27, July
        health=BackupHealth.QUARANTINED,
    )
    x = _backup(
        dt.datetime(2026, 6, 30, 12, 0, tzinfo=dt.timezone.utc),  # week 27, June
        health=BackupHealth.QUARANTINED,
    )
    z = _backup(dt.datetime(2026, 6, 29, 12, 0, tzinfo=dt.timezone.utc))  # wk 27, June
    pruned = backups_to_prune(policy, [w, y, x, z], _NOW)
    assert pruned == [y, x]


@pytest.mark.parametrize(
    ("policy", "pruned_indexes"),
    [
        (RetentionPolicy.from_fields(keep_last=2), [2]),
        (RetentionPolicy.from_fields(daily=1, weekly=1, monthly=2), [1]),
    ],
    ids=["keep_last", "tiered"],
)
def test_all_quarantined_rows_keep_what_health_blind_retention_keeps(
    policy: RetentionPolicy, pruned_indexes: list[int]
) -> None:
    # With no other row to prefer, every slot and bucket goes to the newest
    # quarantined row that fits it, exactly as when health is ignored.
    rows = [
        _backup(_NOW, health=BackupHealth.QUARANTINED),
        _backup(
            dt.datetime(2026, 7, 9, 12, 0, tzinfo=dt.timezone.utc),
            health=BackupHealth.QUARANTINED,
        ),
        _backup(
            dt.datetime(2026, 6, 15, 12, 0, tzinfo=dt.timezone.utc),
            health=BackupHealth.QUARANTINED,
        ),
    ]
    pruned = backups_to_prune(policy, rows, _NOW)
    assert pruned == [rows[i] for i in pruned_indexes]


@pytest.mark.parametrize(
    "source", [BackupSource.MANUAL, BackupSource.UPLOADED, BackupSource.EVENT]
)
@pytest.mark.parametrize("health", [BackupHealth.QUARANTINED, BackupHealth.UNREADABLE])
def test_non_scheduled_rows_are_never_pruned_whatever_their_health(
    source: BackupSource, health: BackupHealth
) -> None:
    # Retention prunes the displaced quarantined scheduled row, never the row
    # of another source beside it.
    policy = RetentionPolicy.from_fields(keep_last=1)
    other_source = _backup(_NOW, source=source, health=health)
    quarantined_scheduled = _backup(
        _NOW - dt.timedelta(hours=1), health=BackupHealth.QUARANTINED
    )
    healthy_scheduled = _backup(_NOW - dt.timedelta(hours=2))
    pruned = backups_to_prune(
        policy, [other_source, quarantined_scheduled, healthy_scheduled], _NOW
    )
    assert pruned == [quarantined_scheduled]


# --- rows with no recorded size (issue #4105) --------------------------------
#
# A row is recorded with its size once its archive exists (issue #4086), so a
# NULL size marks a legacy row: one left dangling with no archive, or one whose
# size was never recorded. Such a row ranks behind every row with a size, a
# quarantined one included, whatever its own health: it keeps only a slot or
# bucket no sized row is left to fill, and never displaces one. Among NULL-size
# rows, age alone decides.


@pytest.mark.parametrize("tier", list(_SINGLE_SLOT_POLICIES))
@pytest.mark.parametrize(
    "health", [BackupHealth.HEALTHY, BackupHealth.UNKNOWN, BackupHealth.UNREADABLE]
)
def test_null_size_row_yields_its_slot_to_an_older_sized_row(
    tier: str, health: BackupHealth
) -> None:
    dangling = _backup(_NOW, health=health, size_bytes=None)
    sized = _backup(_NOW - dt.timedelta(hours=2))
    pruned = backups_to_prune(_SINGLE_SLOT_POLICIES[tier], [dangling, sized], _NOW)
    assert pruned == [dangling]


@pytest.mark.parametrize(
    ("keep_last", "pruned_names"),
    [
        (1, ["dangling_newest", "dangling", "sized_older"]),
        (2, ["dangling_newest", "dangling"]),
        # Only two sized rows for three slots: the newest dangling row takes
        # the third, so keep-N still keeps as many rows as it would by age.
        (3, ["dangling"]),
    ],
)
def test_keep_n_gives_null_size_rows_only_the_slots_sized_rows_leave(
    keep_last: int, pruned_names: list[str]
) -> None:
    # The newest rows are dangling ones, stamped healthy as the #4086 leak left
    # them and unreadable as the sweep marks a missing archive. By age alone
    # they would hold the slots and the prune would delete the sized rows.
    rows = {
        "dangling_newest": _backup(_NOW, size_bytes=None),
        "dangling": _backup(
            _NOW - dt.timedelta(hours=1),
            health=BackupHealth.UNREADABLE,
            size_bytes=None,
        ),
        "sized": _backup(_NOW - dt.timedelta(hours=2)),
        "sized_older": _backup(_NOW - dt.timedelta(hours=3)),
    }
    pruned = backups_to_prune(
        RetentionPolicy.from_fields(keep_last=keep_last), list(rows.values()), _NOW
    )
    assert pruned == [rows[name] for name in pruned_names]


def test_tiered_bucket_keeps_its_sized_row_and_a_null_size_row_fills_its_own() -> None:
    # Today's newer dangling row yields the day to the sized row beside it.
    # Yesterday holds only a dangling row, so it keeps that bucket, while a
    # sized row outside every window is still pruned.
    policy = RetentionPolicy.from_fields(daily=2)
    dangling_today = _backup(_NOW, size_bytes=None)
    sized_today = _backup(_NOW - dt.timedelta(hours=2))
    dangling_yesterday = _backup(
        dt.datetime(2026, 7, 9, 12, 0, tzinfo=dt.timezone.utc), size_bytes=None
    )
    sized_outside = _backup(dt.datetime(2026, 7, 7, 12, 0, tzinfo=dt.timezone.utc))
    pruned = backups_to_prune(
        policy,
        [dangling_today, sized_today, dangling_yesterday, sized_outside],
        _NOW,
    )
    assert pruned == [dangling_today, sized_outside]


@pytest.mark.parametrize(
    ("policy", "pruned_indexes"),
    [
        (RetentionPolicy.from_fields(keep_last=2), [2]),
        (RetentionPolicy.from_fields(daily=1, weekly=1, monthly=2), [1]),
    ],
    ids=["keep_last", "tiered"],
)
def test_all_null_size_rows_keep_what_health_blind_retention_keeps(
    policy: RetentionPolicy, pruned_indexes: list[int]
) -> None:
    # With no sized row to prefer, every slot and bucket goes to the newest
    # row that fits it, exactly as when health is ignored: the quarantined
    # newest row ranks with the healthy one, not behind it.
    rows = [
        _backup(_NOW, health=BackupHealth.QUARANTINED, size_bytes=None),
        _backup(
            dt.datetime(2026, 7, 9, 12, 0, tzinfo=dt.timezone.utc), size_bytes=None
        ),
        _backup(
            dt.datetime(2026, 6, 15, 12, 0, tzinfo=dt.timezone.utc),
            health=BackupHealth.UNREADABLE,
            size_bytes=None,
        ),
    ]
    pruned = backups_to_prune(policy, rows, _NOW)
    assert pruned == [rows[i] for i in pruned_indexes]


@pytest.mark.parametrize("tier", list(_SINGLE_SLOT_POLICIES))
@pytest.mark.parametrize(
    "health",
    [
        BackupHealth.HEALTHY,
        BackupHealth.UNKNOWN,
        BackupHealth.UNREADABLE,
        BackupHealth.QUARANTINED,
    ],
)
def test_sized_quarantined_row_keeps_its_slot_over_a_newer_null_size_row(
    tier: str, health: BackupHealth
) -> None:
    # A quarantined row holds a corrupt world, but its archive exists and can
    # still be force-restored. A NULL-size row may have no archive at all, so
    # it yields the slot or bucket however new it is and whatever its health.
    null_size = _backup(_NOW, health=health, size_bytes=None)
    quarantined = _backup(_NOW - dt.timedelta(hours=2), health=BackupHealth.QUARANTINED)
    pruned = backups_to_prune(
        _SINGLE_SLOT_POLICIES[tier], [null_size, quarantined], _NOW
    )
    assert pruned == [null_size]


@pytest.mark.parametrize(
    ("keep_last", "pruned_names"),
    [
        (1, ["null_size", "quarantined"]),
        (2, ["null_size"]),
        (3, []),
    ],
)
def test_keep_n_fills_sized_rows_then_quarantined_rows_then_null_size_rows(
    keep_last: int, pruned_names: list[str]
) -> None:
    # Newest to oldest: a NULL-size row, a sized quarantined row, a sized
    # healthy row. The slots fill in the reverse order, by rank, not by age.
    rows = {
        "null_size": _backup(_NOW, size_bytes=None),
        "quarantined": _backup(
            _NOW - dt.timedelta(hours=1), health=BackupHealth.QUARANTINED
        ),
        "healthy": _backup(_NOW - dt.timedelta(hours=2)),
    }
    pruned = backups_to_prune(
        RetentionPolicy.from_fields(keep_last=keep_last), list(rows.values()), _NOW
    )
    assert pruned == [rows[name] for name in pruned_names]
