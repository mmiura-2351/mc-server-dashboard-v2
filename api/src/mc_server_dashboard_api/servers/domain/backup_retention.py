"""Backup retention policy and the pure prune-selection function (issue #1841).

A :class:`RetentionPolicy` bounds how many **scheduled** backups a server
retains. It is a union type — exactly one of two forms (owner-confirmed):

- **keep-N**: keep the newest ``keep_last`` scheduled backups (``keep_last >= 1``).
- **tiered**: keep the newest scheduled backup per **UTC calendar day** for the
  last ``daily`` days, per **ISO week** for the last ``weekly`` weeks, and per
  **calendar month** for the last ``monthly`` months — each window anchored at
  ``now`` and including the current day/week/month. Tiers are each ``>= 0`` with
  at least one ``> 0``; a backup kept by *any* tier bucket survives.

Only a healthy row can be kept (issue #1864): a ``QUARANTINED`` or
``UNREADABLE`` scheduled row claims no keep-N slot and no tier bucket, so the
keepers are chosen from the healthy rows alone and a condemned scheduled row is
always pruned — with no healthy row, nothing is kept. ``UNKNOWN`` (a row no
check has read yet) counts as healthy.

:func:`backups_to_prune` is the pure selection: given a policy, a server's
backup rows, and ``now``, it returns the rows the policy prunes. Only
``source=scheduled`` rows are ever candidates — manual / uploaded / event rows
are never auto-deleted. Ordering ties on ``created_at`` break on the backup id,
so the selection is deterministic regardless of input order.

The policy persists as the nullable ``server.backup_retention`` jsonb column
(DATABASE.md Section 7): ``{"keep_last": N}`` or
``{"daily": D, "weekly": W, "monthly": M}``. Standard-library only, no I/O and
no clock (TESTING.md Section 4).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidRetentionPolicyError,
)

_KEEP_LAST_KEYS = frozenset({"keep_last"})
_TIERED_KEYS = frozenset({"daily", "weekly", "monthly"})

# The verdicts that bar a row from claiming a keep-N slot or a tier bucket
# (issue #1864): a check found its world corrupt, or could not read it back.
# UNKNOWN is not one: no check has condemned the row, and the only scheduled
# rows that carry it predate the health column, so barring it would prune every
# such legacy row on the first run, before the integrity sweep reads any of them.
_UNHEALTHY = frozenset({BackupHealth.QUARANTINED, BackupHealth.UNREADABLE})


def _require_int(value: object, name: str) -> int:
    # bool is an int subclass; a policy count must be a genuine integer.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRetentionPolicyError(f"{name} must be an integer")
    return value


@dataclass(frozen=True)
class RetentionPolicy:
    """The scheduled-backup retention policy: keep-N XOR tiered (issue #1841).

    ``keep_last is not None`` selects the keep-N form (the tiers are then all
    zero); ``keep_last is None`` selects the tiered form (at least one tier
    positive). Constructed via :meth:`from_fields` (API input) or
    :meth:`from_json` (the persisted jsonb shape); both raise
    :class:`InvalidRetentionPolicyError` on violation.
    """

    keep_last: int | None = None
    daily: int = 0
    weekly: int = 0
    monthly: int = 0

    def __post_init__(self) -> None:
        for name in ("daily", "weekly", "monthly"):
            value = _require_int(getattr(self, name), name)
            if value < 0:
                raise InvalidRetentionPolicyError(f"{name} must be >= 0")
        if self.keep_last is not None:
            if _require_int(self.keep_last, "keep_last") < 1:
                raise InvalidRetentionPolicyError("keep_last must be >= 1")
            if self.daily or self.weekly or self.monthly:
                raise InvalidRetentionPolicyError(
                    "a policy is keep_last XOR tiered, not both"
                )
        elif not (self.daily or self.weekly or self.monthly):
            raise InvalidRetentionPolicyError("at least one tier must be > 0")

    @classmethod
    def from_fields(
        cls,
        *,
        keep_last: int | None = None,
        daily: int | None = None,
        weekly: int | None = None,
        monthly: int | None = None,
    ) -> RetentionPolicy:
        """Build a policy from optional API fields (issue #1841).

        ``keep_last`` alone selects the keep-N form; any tier field selects the
        tiered form (omitted tiers default to 0). Mixing the two forms, or
        supplying neither, is invalid.
        """

        tiers_given = any(v is not None for v in (daily, weekly, monthly))
        if keep_last is not None and tiers_given:
            raise InvalidRetentionPolicyError(
                "a policy is keep_last XOR tiered, not both"
            )
        if keep_last is None and not tiers_given:
            raise InvalidRetentionPolicyError("one of keep_last or a tier is required")
        if keep_last is not None:
            return cls(keep_last=keep_last)
        return cls(daily=daily or 0, weekly=weekly or 0, monthly=monthly or 0)

    @classmethod
    def from_json(cls, data: object) -> RetentionPolicy:
        """Parse the persisted jsonb shape, rejecting anything non-canonical.

        Exactly ``{"keep_last": N}`` or ``{"daily": D, "weekly": W,
        "monthly": M}`` — unknown keys, missing tier keys, or non-integer values
        are refused, so a hand-edited row surfaces loudly rather than silently
        pruning the wrong set.
        """

        if not isinstance(data, dict):
            raise InvalidRetentionPolicyError("policy must be a JSON object")
        keys = set(data)
        if keys == _KEEP_LAST_KEYS:
            return cls(keep_last=_require_int(data["keep_last"], "keep_last"))
        if keys == _TIERED_KEYS:
            return cls(
                daily=_require_int(data["daily"], "daily"),
                weekly=_require_int(data["weekly"], "weekly"),
                monthly=_require_int(data["monthly"], "monthly"),
            )
        raise InvalidRetentionPolicyError(
            "policy must be {keep_last} or {daily, weekly, monthly}"
        )

    def to_json(self) -> dict[str, int]:
        """The canonical persisted shape (the ``backup_retention`` jsonb value)."""

        if self.keep_last is not None:
            return {"keep_last": self.keep_last}
        return {"daily": self.daily, "weekly": self.weekly, "monthly": self.monthly}


def _newest_first(backups: list[Backup]) -> list[Backup]:
    # Descending by created_at; ties break on the id string so the selection is
    # deterministic regardless of the repository's tie order.
    return sorted(backups, key=lambda b: (b.created_at, str(b.id.value)), reverse=True)


def _window_days(now_date: dt.date, days: int) -> set[dt.date]:
    return {now_date - dt.timedelta(days=i) for i in range(days)}


def _window_weeks(now_date: dt.date, weeks: int) -> set[tuple[int, int]]:
    monday = now_date - dt.timedelta(days=now_date.isoweekday() - 1)
    return {(monday - dt.timedelta(weeks=i)).isocalendar()[:2] for i in range(weeks)}


def _window_months(now_date: dt.date, months: int) -> set[tuple[int, int]]:
    year, month = now_date.year, now_date.month
    window: set[tuple[int, int]] = set()
    for _ in range(months):
        window.add((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return window


def backups_to_prune(
    policy: RetentionPolicy, backups: Sequence[Backup], now: dt.datetime
) -> list[Backup]:
    """Return the ``source=scheduled`` rows ``policy`` prunes, newest-first.

    Pure: no I/O, no clock — ``now`` anchors the tiered windows (UTC
    boundaries). Manual / uploaded / event rows are never candidates. Keep-N
    keeps the newest N healthy scheduled rows; tiered keeps the newest healthy
    scheduled row per day/week/month bucket inside the configured windows.
    Every other scheduled row is pruned, a quarantined or unreadable one always.
    """

    scheduled = _newest_first(
        [b for b in backups if b.source is BackupSource.SCHEDULED]
    )
    claimants = [b for b in scheduled if b.health not in _UNHEALTHY]
    if policy.keep_last is not None:
        kept = {b.id for b in claimants[: policy.keep_last]}
        return [b for b in scheduled if b.id not in kept]

    now_date = now.astimezone(dt.timezone.utc).date()
    days = _window_days(now_date, policy.daily)
    weeks = _window_weeks(now_date, policy.weekly)
    months = _window_months(now_date, policy.monthly)
    kept = set()
    claimed_days: set[dt.date] = set()
    claimed_weeks: set[tuple[int, int]] = set()
    claimed_months: set[tuple[int, int]] = set()
    for backup in claimants:  # newest first, so the first claimant is the newest
        created = backup.created_at.astimezone(dt.timezone.utc).date()
        if created in days and created not in claimed_days:
            claimed_days.add(created)
            kept.add(backup.id)
        week = created.isocalendar()[:2]
        if week in weeks and week not in claimed_weeks:
            claimed_weeks.add(week)
            kept.add(backup.id)
        month = (created.year, created.month)
        if month in months and month not in claimed_months:
            claimed_months.add(month)
            kept.add(backup.id)
    return [b for b in scheduled if b.id not in kept]
