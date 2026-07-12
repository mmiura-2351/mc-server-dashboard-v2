"""Domain model for the general scheduler (epic #649, issue #1835).

A :class:`Schedule` is a per-server recurring action (DATABASE.md's ``schedule``
table): run a console command, start/stop/restart the server, or take a backup,
on a cron expression XOR a fixed interval (:class:`Cadence`), in a per-schedule
IANA timezone. A :class:`ScheduleRun` records one execution outcome (the
``schedule_run`` table).

Standard-library only, no I/O and no clock (TESTING.md Section 4). Cron *syntax*
and cron next-occurrence math need a cron engine, so they live behind the
:class:`~.next_run_calculator.NextRunCalculator` Port (cronsim, adapters only);
interval next-occurrence math is pure and lives here, with a deterministic
per-schedule jitter so schedules sharing an interval do not fire in lockstep
(the :mod:`.backup_schedule` thundering-herd pattern).
"""

from __future__ import annotations

import datetime as dt
import enum
import hashlib
import uuid
import zoneinfo
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.errors import (
    InvalidScheduleCadenceError,
    InvalidScheduleError,
    InvalidScheduleNameError,
    InvalidSchedulePayloadError,
    InvalidScheduleTimezoneError,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId

DEFAULT_TIMEZONE = "UTC"

# Warning-step bounds for stop/restart payloads (owner-confirmed, epic #649):
# at most five steps, each a positive whole-minute offset of at most two hours.
MAX_WARNING_STEPS = 5
MAX_WARNING_OFFSET_MINUTES = 120

# The interval-cadence floor (issue #1838 review): one minute, matching cron's
# granularity (the other cadence form cannot go finer either). A sub-minute
# interval would also undercut the runner's tick resolution and its fixed
# late-run staleness gate, so occurrences could be judged stale before the loop
# ever saw them. Validated here as the single source of truth; the CRUD layer
# surfaces it as a 422 automatically.
MIN_INTERVAL_SECONDS = 60

# The interval jitter is at most this fraction of the interval, so it staggers
# schedules sharing a cadence without meaningfully changing that cadence (the
# ``backup_schedule.JITTER_FRACTION`` pattern).
SCHEDULE_JITTER_FRACTION = 0.1


@dataclass(frozen=True)
class ScheduleId:
    """The identity of a :class:`Schedule` (a UUID primary key)."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> ScheduleId:
        """Generate a fresh, random schedule id."""

        return cls(uuid.uuid4())


@dataclass(frozen=True)
class ScheduleRunId:
    """The identity of a :class:`ScheduleRun` (a UUID primary key)."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> ScheduleRunId:
        """Generate a fresh, random run id."""

        return cls(uuid.uuid4())


class ScheduleAction(enum.Enum):
    """What a schedule does when it fires (``ck_schedule_action`` CHECK enum).

    ``COMMAND`` sends a console command line; ``START`` / ``STOP`` / ``RESTART``
    drive the server lifecycle; ``BACKUP`` takes a backup (the FR-BAK-3 cadence
    folds into this action at #1840).
    """

    COMMAND = "command"
    START = "start"
    STOP = "stop"
    RESTART = "restart"
    BACKUP = "backup"


class ScheduleRunOutcome(enum.Enum):
    """How one execution ended (``ck_schedule_run_outcome`` CHECK enum).

    ``SKIPPED`` records a fired occurrence whose precondition was unmet (e.g.
    stopping a server that is not running) — an honest history entry, not a
    failure.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Cadence:
    """When a schedule fires: a cron expression XOR a fixed interval.

    Exactly one of ``cron`` / ``interval_seconds`` is set (the table's
    ``ck_schedule_cadence_xor`` CHECK). Cron *syntax* is validated by the
    ``NextRunCalculator`` Port (the cron engine lives in adapters); here the
    expression is only required to be non-blank. An interval is a whole number
    of seconds of at least :data:`MIN_INTERVAL_SECONDS`.
    """

    cron: str | None
    interval_seconds: int | None

    def __post_init__(self) -> None:
        if (self.cron is None) == (self.interval_seconds is None):
            raise InvalidScheduleCadenceError(
                "exactly one of cron / interval_seconds must be set"
            )
        if self.cron is not None and not self.cron.strip():
            raise InvalidScheduleCadenceError("cron expression must not be blank")
        if self.interval_seconds is not None and (
            isinstance(self.interval_seconds, bool)
            or self.interval_seconds < MIN_INTERVAL_SECONDS
        ):
            raise InvalidScheduleCadenceError(
                f"interval must be at least {MIN_INTERVAL_SECONDS} seconds"
            )

    @classmethod
    def from_cron(cls, expr: str) -> Cadence:
        """A cron cadence (5-field expression, validated by the calculator Port)."""

        return cls(cron=expr, interval_seconds=None)

    @classmethod
    def from_interval(cls, seconds: int) -> Cadence:
        """A fixed-interval cadence of ``seconds`` (>= ``MIN_INTERVAL_SECONDS``)."""

        return cls(cron=None, interval_seconds=seconds)


@dataclass(frozen=True)
class WarningStep:
    """One pre-action player warning for a stop/restart schedule (issue #1839).

    Broadcast as a fixed ``say <message>`` ``offset_minutes`` before the main
    action fires. Offsets are positive whole minutes of at most
    ``MAX_WARNING_OFFSET_MINUTES``; the message is non-blank.
    """

    offset_minutes: int
    message: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.offset_minutes, bool)
            or self.offset_minutes < 1
            or self.offset_minutes > MAX_WARNING_OFFSET_MINUTES
        ):
            raise InvalidSchedulePayloadError(
                f"warning offset must be 1..{MAX_WARNING_OFFSET_MINUTES} minutes"
            )
        if not self.message.strip():
            raise InvalidSchedulePayloadError("warning message must not be blank")


@dataclass
class Schedule:
    """Row of the ``schedule`` table: a per-server recurring action.

    The per-action payload is carried as typed fields — ``command`` (the console
    line, ``COMMAND`` only) and ``warning_steps`` (``STOP`` / ``RESTART`` only,
    optional) — and serialized into the ``payload`` jsonb column by the
    repository adapter. ``next_run_at`` is the persisted due instant the runner
    polls on; it is ``None`` exactly while the schedule is disabled.
    ``created_by`` is a soft actor reference (no FK) so the row survives the
    user's deletion, mirroring ``backup.created_by``.
    """

    id: ScheduleId
    server_id: ServerId
    name: str
    action: ScheduleAction
    cadence: Cadence
    enabled: bool
    created_at: dt.datetime
    updated_at: dt.datetime
    timezone: str = DEFAULT_TIMEZONE
    command: str | None = None
    warning_steps: tuple[WarningStep, ...] = ()
    next_run_at: dt.datetime | None = None
    last_run_at: dt.datetime | None = None
    created_by: uuid.UUID | None = None

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        if not self.name:
            raise InvalidScheduleNameError("schedule name must not be blank")
        _validate_timezone(self.timezone)
        self._validate_payload()
        if not self.enabled and self.next_run_at is not None:
            raise InvalidScheduleError(
                "a disabled schedule must not carry a next_run_at"
            )

    def _validate_payload(self) -> None:
        if self.action is ScheduleAction.COMMAND:
            if self.command is None or not self.command.strip():
                raise InvalidSchedulePayloadError(
                    "the command action requires a command line"
                )
            if "\n" in self.command or "\r" in self.command:
                raise InvalidSchedulePayloadError("command must be a single line")
        elif self.command is not None:
            raise InvalidSchedulePayloadError(
                f"the {self.action.value} action carries no command"
            )
        if self.warning_steps and self.action not in (
            ScheduleAction.STOP,
            ScheduleAction.RESTART,
        ):
            raise InvalidSchedulePayloadError(
                f"the {self.action.value} action carries no warning steps"
            )
        if len(self.warning_steps) > MAX_WARNING_STEPS:
            raise InvalidSchedulePayloadError(
                f"at most {MAX_WARNING_STEPS} warning steps"
            )
        offsets = [step.offset_minutes for step in self.warning_steps]
        if len(set(offsets)) != len(offsets):
            raise InvalidSchedulePayloadError("warning offsets must be distinct")


@dataclass
class ScheduleRun:
    """Row of the ``schedule_run`` table: one recorded execution of a schedule.

    Written by the runner after the occurrence completes, so both timestamps are
    always known. ``detail`` is an optional, already-sanitized outcome note (a
    failure category, a skip reason) — never a raw worker/OS message.
    """

    id: ScheduleRunId
    schedule_id: ScheduleId
    started_at: dt.datetime
    finished_at: dt.datetime
    outcome: ScheduleRunOutcome
    detail: str | None


def _validate_timezone(name: str) -> None:
    try:
        zoneinfo.ZoneInfo(name)
    except (ValueError, zoneinfo.ZoneInfoNotFoundError):
        raise InvalidScheduleTimezoneError(name) from None


def interval_jitter_seconds(schedule_id: ScheduleId, *, interval_seconds: int) -> float:
    """Return a deterministic per-schedule offset in ``[0, interval * fraction)``.

    Derived from the schedule id via a stable hash, so it survives restarts and
    differs across schedules, spreading the due instants of schedules that share
    an interval (the ``backup_schedule.jitter_seconds`` thundering-herd guard).
    """

    digest = hashlib.sha256(schedule_id.value.bytes).digest()
    # Map the first 8 digest bytes to a fraction in [0, 1), then scale to the bound.
    fraction = int.from_bytes(digest[:8], "big") / 2**64
    return fraction * interval_seconds * SCHEDULE_JITTER_FRACTION


_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
_MICROSECOND = dt.timedelta(microseconds=1)


def next_interval_run(
    schedule_id: ScheduleId, *, interval_seconds: int, after: dt.datetime
) -> dt.datetime:
    """Return the first interval occurrence strictly after ``after`` (UTC).

    Occurrences sit on an epoch-anchored grid shifted by the per-schedule
    jitter — ``epoch + k * interval + jitter`` — so the cadence is stable
    regardless of when runs actually execute (no drift from execution latency),
    mirroring how a cron cadence is anchored to the calendar. The arithmetic is
    exact integer microseconds (the datetime resolution): with floats, an
    occurrence recomputed *from* a previous occurrence could round back onto
    it and violate the strictly-after contract. ``after`` must be
    timezone-aware; an interval cadence measures absolute elapsed time, so the
    schedule's display timezone plays no part here.
    """

    jitter_us = round(
        interval_jitter_seconds(schedule_id, interval_seconds=interval_seconds)
        * 1_000_000
    )
    interval_us = interval_seconds * 1_000_000
    after_us = (after - _EPOCH) // _MICROSECOND
    k = (after_us - jitter_us) // interval_us + 1
    return _EPOCH + (k * interval_us + jitter_us) * _MICROSECOND
