"""Unit tests for the schedule domain model (epic #649, issue #1835).

Pure, stdlib-only domain: the ``Schedule`` entity invariants (name, timezone,
per-action payload), the cron-XOR-interval cadence value object, and the
interval next-run math with deterministic per-schedule jitter.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.servers.domain.errors import (
    InvalidScheduleCadenceError,
    InvalidScheduleError,
    InvalidScheduleNameError,
    InvalidSchedulePayloadError,
    InvalidScheduleTimezoneError,
)
from mc_server_dashboard_api.servers.domain.schedule import (
    MAX_WARNING_OFFSET_MINUTES,
    MAX_WARNING_STEPS,
    SCHEDULE_JITTER_FRACTION,
    Cadence,
    Schedule,
    ScheduleAction,
    ScheduleId,
    ScheduleRun,
    ScheduleRunId,
    ScheduleRunOutcome,
    WarningStep,
    interval_jitter_seconds,
    next_interval_run,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId

_NOW = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc)


def _schedule(
    *,
    action: ScheduleAction = ScheduleAction.BACKUP,
    name: str = "nightly",
    cadence: Cadence | None = None,
    timezone: str = "UTC",
    enabled: bool = False,
    command: str | None = None,
    warning_steps: tuple[WarningStep, ...] = (),
    next_run_at: dt.datetime | None = None,
) -> Schedule:
    return Schedule(
        id=ScheduleId.new(),
        server_id=ServerId(uuid.uuid4()),
        name=name,
        action=action,
        cadence=cadence or Cadence.from_interval(3600),
        enabled=enabled,
        created_at=_NOW,
        updated_at=_NOW,
        timezone=timezone,
        command=command,
        warning_steps=warning_steps,
        next_run_at=next_run_at,
    )


# --- enums -------------------------------------------------------------------


def test_action_enum_matches_check_constraint_values() -> None:
    assert {a.value for a in ScheduleAction} == {
        "command",
        "start",
        "stop",
        "restart",
        "backup",
    }


def test_run_outcome_enum_matches_check_constraint_values() -> None:
    assert {o.value for o in ScheduleRunOutcome} == {"success", "failure", "skipped"}


# --- cadence (cron XOR interval) ----------------------------------------------


def test_cron_cadence_round_trips() -> None:
    cadence = Cadence.from_cron("*/5 * * * *")
    assert cadence.cron == "*/5 * * * *"
    assert cadence.interval_seconds is None


def test_interval_cadence_round_trips() -> None:
    cadence = Cadence.from_interval(3600)
    assert cadence.cron is None
    assert cadence.interval_seconds == 3600


def test_cadence_rejects_both_cron_and_interval() -> None:
    with pytest.raises(InvalidScheduleCadenceError):
        Cadence(cron="* * * * *", interval_seconds=60)


def test_cadence_rejects_neither_cron_nor_interval() -> None:
    with pytest.raises(InvalidScheduleCadenceError):
        Cadence(cron=None, interval_seconds=None)


def test_cadence_rejects_blank_cron() -> None:
    with pytest.raises(InvalidScheduleCadenceError):
        Cadence.from_cron("   ")


@pytest.mark.parametrize("bad", [0, -60, True])
def test_cadence_rejects_non_positive_interval(bad: int) -> None:
    with pytest.raises(InvalidScheduleCadenceError):
        Cadence.from_interval(bad)


@pytest.mark.parametrize("bad", [1, 59])
def test_cadence_rejects_sub_minute_interval(bad: int) -> None:
    # The 60-second floor (issue #1838 review): a sub-tick interval would never
    # fire (the runner's staleness gate) and cron is minute-granular anyway.
    with pytest.raises(InvalidScheduleCadenceError):
        Cadence.from_interval(bad)


def test_cadence_accepts_the_minimum_interval() -> None:
    assert Cadence.from_interval(60).interval_seconds == 60


# --- schedule invariants -------------------------------------------------------


def test_name_is_trimmed() -> None:
    assert _schedule(name="  nightly  ").name == "nightly"


def test_blank_name_rejected() -> None:
    with pytest.raises(InvalidScheduleNameError):
        _schedule(name="   ")


def test_timezone_defaults_to_utc() -> None:
    schedule = Schedule(
        id=ScheduleId.new(),
        server_id=ServerId(uuid.uuid4()),
        name="nightly",
        action=ScheduleAction.BACKUP,
        cadence=Cadence.from_interval(3600),
        enabled=False,
        created_at=_NOW,
        updated_at=_NOW,
    )
    assert schedule.timezone == "UTC"


def test_iana_timezone_accepted() -> None:
    assert _schedule(timezone="Europe/Berlin").timezone == "Europe/Berlin"


@pytest.mark.parametrize("bad", ["Mars/Olympus", "UTC+9", ""])
def test_unknown_timezone_rejected(bad: str) -> None:
    with pytest.raises(InvalidScheduleTimezoneError):
        _schedule(timezone=bad)


def test_disabled_schedule_must_carry_no_next_run() -> None:
    with pytest.raises(InvalidScheduleError):
        _schedule(enabled=False, next_run_at=_NOW)


def test_enabled_schedule_carries_next_run() -> None:
    schedule = _schedule(enabled=True, next_run_at=_NOW)
    assert schedule.next_run_at == _NOW


# --- per-action payload ---------------------------------------------------------


def test_command_action_requires_command_line() -> None:
    schedule = _schedule(action=ScheduleAction.COMMAND, command="say hello world")
    assert schedule.command == "say hello world"


def test_command_action_without_command_rejected() -> None:
    with pytest.raises(InvalidSchedulePayloadError):
        _schedule(action=ScheduleAction.COMMAND, command=None)


@pytest.mark.parametrize("bad", ["", "   ", "say hi\nsay bye", "say hi\r"])
def test_command_must_be_a_non_blank_single_line(bad: str) -> None:
    with pytest.raises(InvalidSchedulePayloadError):
        _schedule(action=ScheduleAction.COMMAND, command=bad)


@pytest.mark.parametrize(
    "action",
    [
        ScheduleAction.START,
        ScheduleAction.STOP,
        ScheduleAction.RESTART,
        ScheduleAction.BACKUP,
    ],
)
def test_non_command_action_rejects_command(action: ScheduleAction) -> None:
    with pytest.raises(InvalidSchedulePayloadError):
        _schedule(action=action, command="say hi")


@pytest.mark.parametrize("action", [ScheduleAction.STOP, ScheduleAction.RESTART])
def test_stop_and_restart_accept_warning_steps(action: ScheduleAction) -> None:
    steps = (
        WarningStep(offset_minutes=10, message="restarting in 10 minutes"),
        WarningStep(offset_minutes=1, message="restarting in 1 minute"),
    )
    assert _schedule(action=action, warning_steps=steps).warning_steps == steps


@pytest.mark.parametrize(
    "action",
    [ScheduleAction.COMMAND, ScheduleAction.START, ScheduleAction.BACKUP],
)
def test_other_actions_reject_warning_steps(action: ScheduleAction) -> None:
    command = "say hi" if action is ScheduleAction.COMMAND else None
    with pytest.raises(InvalidSchedulePayloadError):
        _schedule(
            action=action,
            command=command,
            warning_steps=(WarningStep(offset_minutes=5, message="soon"),),
        )


def test_more_than_five_warning_steps_rejected() -> None:
    steps = tuple(
        WarningStep(offset_minutes=m, message=f"in {m}") for m in (1, 2, 3, 4, 5, 6)
    )
    assert len(steps) == MAX_WARNING_STEPS + 1
    with pytest.raises(InvalidSchedulePayloadError):
        _schedule(action=ScheduleAction.STOP, warning_steps=steps)


def test_duplicate_warning_offsets_rejected() -> None:
    steps = (
        WarningStep(offset_minutes=5, message="a"),
        WarningStep(offset_minutes=5, message="b"),
    )
    with pytest.raises(InvalidSchedulePayloadError):
        _schedule(action=ScheduleAction.STOP, warning_steps=steps)


@pytest.mark.parametrize("bad", [0, -5, MAX_WARNING_OFFSET_MINUTES + 1, True])
def test_warning_offset_out_of_range_rejected(bad: int) -> None:
    with pytest.raises(InvalidSchedulePayloadError):
        WarningStep(offset_minutes=bad, message="soon")


def test_blank_warning_message_rejected() -> None:
    with pytest.raises(InvalidSchedulePayloadError):
        WarningStep(offset_minutes=5, message="   ")


@pytest.mark.parametrize("bad", ["stop\nsay op", "stop\rsay op"])
def test_multiline_warning_message_rejected(bad: str) -> None:
    # The runner turns the message into a console line (``say <message>``), so a
    # newline must never smuggle a second command — same posture as ``command``.
    with pytest.raises(InvalidSchedulePayloadError):
        WarningStep(offset_minutes=5, message=bad)


# --- schedule runs ---------------------------------------------------------------


def test_schedule_run_round_trips_fields() -> None:
    run = ScheduleRun(
        id=ScheduleRunId.new(),
        schedule_id=ScheduleId.new(),
        started_at=_NOW,
        finished_at=_NOW + dt.timedelta(seconds=3),
        outcome=ScheduleRunOutcome.SUCCESS,
        detail=None,
    )
    assert run.outcome is ScheduleRunOutcome.SUCCESS
    assert run.finished_at - run.started_at == dt.timedelta(seconds=3)


# --- interval next-run math -------------------------------------------------------


def test_jitter_is_deterministic_per_schedule() -> None:
    schedule_id = ScheduleId.new()
    a = interval_jitter_seconds(schedule_id, interval_seconds=3600)
    b = interval_jitter_seconds(schedule_id, interval_seconds=3600)
    assert a == b


def test_jitter_within_bound() -> None:
    schedule_id = ScheduleId.new()
    interval = 3600
    offset = interval_jitter_seconds(schedule_id, interval_seconds=interval)
    assert 0 <= offset < interval * SCHEDULE_JITTER_FRACTION


def test_jitter_differs_across_schedules() -> None:
    a = ScheduleId(uuid.UUID("00000000-0000-0000-0000-000000000001"))
    b = ScheduleId(uuid.UUID("00000000-0000-0000-0000-000000000002"))
    assert interval_jitter_seconds(a, interval_seconds=3600) != interval_jitter_seconds(
        b, interval_seconds=3600
    )


def test_next_interval_run_is_strictly_after() -> None:
    schedule_id = ScheduleId.new()
    first = next_interval_run(schedule_id, interval_seconds=3600, after=_NOW)
    assert first > _NOW
    assert first - _NOW <= dt.timedelta(seconds=3600)
    # Asking again from the returned instant advances (strictly-after contract):
    # ``first`` IS an occurrence, so with float math a microsecond-rounded
    # recompute could land back on it (the regression the exact integer
    # arithmetic pins down).
    second = next_interval_run(schedule_id, interval_seconds=3600, after=first)
    assert second > first


def test_next_interval_runs_are_one_interval_apart() -> None:
    schedule_id = ScheduleId.new()
    first = next_interval_run(schedule_id, interval_seconds=3600, after=_NOW)
    second = next_interval_run(schedule_id, interval_seconds=3600, after=first)
    assert second - first == dt.timedelta(seconds=3600)


def test_next_interval_run_sits_on_jittered_epoch_grid() -> None:
    schedule_id = ScheduleId.new()
    interval = 3600
    jitter = interval_jitter_seconds(schedule_id, interval_seconds=interval)
    occurrence = next_interval_run(schedule_id, interval_seconds=interval, after=_NOW)
    # The applied jitter is quantized to whole microseconds, so the remainder
    # sits within quantization error of a grid multiple (either side of it).
    remainder = (occurrence.timestamp() - jitter) % interval
    assert min(remainder, interval - remainder) < 1e-3


def test_next_interval_run_is_utc_aware() -> None:
    occurrence = next_interval_run(ScheduleId.new(), interval_seconds=60, after=_NOW)
    assert occurrence.tzinfo == dt.timezone.utc
