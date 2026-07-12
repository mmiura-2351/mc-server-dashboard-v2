"""Use-case tests for schedule CRUD against in-memory fakes (issue #1837).

Covers the CRUD lifecycle, the two-layer write gate (``schedule:manage`` plus the
action's own permission) and its anti-escalation posture, cross-scope not-found,
per-server name uniqueness, the ``next_run_at`` recompute-on-enable / null-on-
disable invariant, and the validation-error families. Uses the real interval /
cronsim next-run math (pure, no I/O) so the computed due instants are exercised.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable

import pytest

from mc_server_dashboard_api.servers.adapters.cronsim_next_run_calculator import (
    CronsimNextRunCalculator,
)
from mc_server_dashboard_api.servers.application.schedules import (
    CreateSchedule,
    DeleteSchedule,
    ListScheduleRuns,
    ListSchedules,
    ReadSchedule,
    UpdateSchedule,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidCronExpressionError,
    InvalidScheduleCadenceError,
    InvalidSchedulePayloadError,
    InvalidScheduleTimezoneError,
    PermissionDeniedError,
    ScheduleNameAlreadyExistsError,
    ScheduleNotFoundError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.schedule import (
    Cadence,
    Schedule,
    ScheduleAction,
    ScheduleId,
    ScheduleRun,
    ScheduleRunId,
    ScheduleRunOutcome,
    WarningStep,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.servers.fakes import FakeClock, FakeUnitOfWork

_NOW = dt.datetime(2026, 7, 11, 12, 0, tzinfo=dt.timezone.utc)
_COMMUNITY = CommunityId(uuid.uuid4())
_OTHER_COMMUNITY = CommunityId(uuid.uuid4())


def _server(*, community: CommunityId = _COMMUNITY) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=community,
        name=ServerName("srv"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=_NOW,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _uow(*, community: CommunityId = _COMMUNITY) -> tuple[FakeUnitOfWork, Server]:
    server = _server(community=community)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    return uow, server


def _authorizer(allowed: set[str]) -> Callable[[str], Awaitable[bool]]:
    async def authorize(code: str) -> bool:
        return code in allowed

    return authorize


async def _allow(_code: str) -> bool:
    return True


def _create(uow: FakeUnitOfWork) -> CreateSchedule:
    return CreateSchedule(
        uow=uow, clock=FakeClock(_NOW), calculator=CronsimNextRunCalculator()
    )


def _update(uow: FakeUnitOfWork) -> UpdateSchedule:
    return UpdateSchedule(
        uow=uow, clock=FakeClock(_NOW), calculator=CronsimNextRunCalculator()
    )


def _seed_schedule(
    uow: FakeUnitOfWork,
    server_id: ServerId,
    *,
    name: str = "nightly",
    action: ScheduleAction = ScheduleAction.START,
    enabled: bool = True,
    command: str | None = None,
    warning_steps: tuple[WarningStep, ...] = (),
) -> Schedule:
    schedule = Schedule(
        id=ScheduleId.new(),
        server_id=server_id,
        name=name,
        action=action,
        cadence=Cadence.from_interval(3600),
        enabled=enabled,
        created_at=_NOW,
        updated_at=_NOW,
        command=command,
        warning_steps=warning_steps,
        next_run_at=_NOW if enabled else None,
    )
    uow.schedules.seed(schedule)
    return schedule


# --- create ----------------------------------------------------------------


async def test_create_persists_commits_and_computes_next_run() -> None:
    uow, server = _uow()
    actor = uuid.uuid4()
    schedule = await _create(uow)(
        community_id=_COMMUNITY,
        server_id=server.id,
        authorize=_allow,
        name="nightly restart",
        action=ScheduleAction.RESTART,
        interval_seconds=3600,
        created_by=actor,
    )
    assert uow.commits == 1
    assert await uow.schedules.get_by_id(schedule.id) is not None
    assert schedule.next_run_at is not None and schedule.next_run_at > _NOW
    assert schedule.created_by == actor


async def test_create_disabled_schedule_has_null_next_run() -> None:
    uow, server = _uow()
    schedule = await _create(uow)(
        community_id=_COMMUNITY,
        server_id=server.id,
        authorize=_allow,
        name="paused",
        action=ScheduleAction.START,
        interval_seconds=3600,
        enabled=False,
    )
    assert schedule.next_run_at is None


async def test_create_cron_schedule_computes_next_run() -> None:
    uow, server = _uow()
    schedule = await _create(uow)(
        community_id=_COMMUNITY,
        server_id=server.id,
        authorize=_allow,
        name="cron",
        action=ScheduleAction.BACKUP,
        cron="0 3 * * *",
        timezone="UTC",
    )
    # 03:00 UTC the day after _NOW (12:00), the first occurrence strictly after.
    assert schedule.next_run_at == dt.datetime(
        2026, 7, 12, 3, 0, tzinfo=dt.timezone.utc
    )


async def test_create_command_schedule_requires_server_command() -> None:
    # Anti-escalation: schedule:manage alone cannot create a command schedule.
    uow, server = _uow()
    with pytest.raises(PermissionDeniedError) as exc:
        await _create(uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            authorize=_authorizer({"schedule:manage"}),
            name="broadcast",
            action=ScheduleAction.COMMAND,
            command="say hi",
            interval_seconds=3600,
        )
    assert exc.value.permission == "server:command"
    assert uow.commits == 0


async def test_create_requires_schedule_manage_first() -> None:
    uow, server = _uow()
    with pytest.raises(PermissionDeniedError) as exc:
        await _create(uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            authorize=_authorizer({"server:command"}),
            name="broadcast",
            action=ScheduleAction.COMMAND,
            command="say hi",
            interval_seconds=3600,
        )
    assert exc.value.permission == "schedule:manage"


async def test_create_backup_schedule_requires_backup_schedule() -> None:
    uow, server = _uow()
    with pytest.raises(PermissionDeniedError) as exc:
        await _create(uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            authorize=_authorizer({"schedule:manage"}),
            name="nightly backup",
            action=ScheduleAction.BACKUP,
            cron="0 3 * * *",
        )
    assert exc.value.permission == "backup:schedule"


async def test_create_stop_with_warnings_does_not_need_server_command() -> None:
    # A stop/restart warning is a fixed ``say`` broadcast, not a console command,
    # so it needs only server:stop, never server:command.
    uow, server = _uow()
    schedule = await _create(uow)(
        community_id=_COMMUNITY,
        server_id=server.id,
        authorize=_authorizer({"schedule:manage", "server:stop"}),
        name="evening stop",
        action=ScheduleAction.STOP,
        interval_seconds=3600,
        warning_steps=[(10, "Stopping in 10 minutes"), (1, "Stopping now")],
    )
    assert len(schedule.warning_steps) == 2


async def test_create_on_cross_community_server_is_not_found() -> None:
    uow, server = _uow(community=_OTHER_COMMUNITY)
    with pytest.raises(ServerNotFoundError):
        await _create(uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            authorize=_allow,
            name="x",
            action=ScheduleAction.START,
            interval_seconds=3600,
        )


async def test_create_invalid_cron_is_rejected() -> None:
    uow, server = _uow()
    with pytest.raises(InvalidCronExpressionError):
        await _create(uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            authorize=_allow,
            name="x",
            action=ScheduleAction.START,
            cron="not a cron",
        )


async def test_create_rejects_both_cron_and_interval() -> None:
    uow, server = _uow()
    with pytest.raises(InvalidScheduleCadenceError):
        await _create(uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            authorize=_allow,
            name="x",
            action=ScheduleAction.START,
            cron="0 3 * * *",
            interval_seconds=3600,
        )


async def test_create_command_without_command_line_is_payload_error() -> None:
    uow, server = _uow()
    with pytest.raises(InvalidSchedulePayloadError):
        await _create(uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            authorize=_allow,
            name="x",
            action=ScheduleAction.COMMAND,
            interval_seconds=3600,
        )


async def test_create_rejects_unknown_timezone() -> None:
    uow, server = _uow()
    with pytest.raises(InvalidScheduleTimezoneError):
        await _create(uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            authorize=_allow,
            name="x",
            action=ScheduleAction.START,
            interval_seconds=3600,
            timezone="Mars/Olympus",
        )


async def test_create_duplicate_name_on_server_conflicts() -> None:
    uow, server = _uow()
    _seed_schedule(uow, server.id, name="nightly")
    with pytest.raises(ScheduleNameAlreadyExistsError):
        await _create(uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            authorize=_allow,
            name="nightly",
            action=ScheduleAction.START,
            interval_seconds=3600,
        )


# --- update ----------------------------------------------------------------


async def test_update_recomputes_next_run_on_enable() -> None:
    uow, server = _uow()
    seeded = _seed_schedule(uow, server.id, action=ScheduleAction.START, enabled=False)
    updated = await _update(uow)(
        community_id=_COMMUNITY,
        server_id=server.id,
        schedule_id=seeded.id,
        authorize=_allow,
        enabled=True,
    )
    assert updated.enabled is True
    assert updated.next_run_at is not None and updated.next_run_at > _NOW


async def test_update_disable_clears_next_run() -> None:
    uow, server = _uow()
    seeded = _seed_schedule(uow, server.id, action=ScheduleAction.START, enabled=True)
    updated = await _update(uow)(
        community_id=_COMMUNITY,
        server_id=server.id,
        schedule_id=seeded.id,
        authorize=_allow,
        enabled=False,
    )
    assert updated.enabled is False
    assert updated.next_run_at is None


async def test_update_command_schedule_requires_server_command() -> None:
    uow, server = _uow()
    seeded = _seed_schedule(
        uow, server.id, action=ScheduleAction.COMMAND, command="say hi"
    )
    with pytest.raises(PermissionDeniedError) as exc:
        await _update(uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            schedule_id=seeded.id,
            authorize=_authorizer({"schedule:manage"}),
            enabled=False,
        )
    assert exc.value.permission == "server:command"


async def test_update_clears_warning_steps_with_empty_list() -> None:
    uow, server = _uow()
    seeded = _seed_schedule(
        uow,
        server.id,
        action=ScheduleAction.STOP,
        warning_steps=(WarningStep(offset_minutes=5, message="soon"),),
    )
    updated = await _update(uow)(
        community_id=_COMMUNITY,
        server_id=server.id,
        schedule_id=seeded.id,
        authorize=_allow,
        warning_steps=[],
    )
    assert updated.warning_steps == ()


async def test_update_omitting_warning_steps_keeps_them() -> None:
    uow, server = _uow()
    seeded = _seed_schedule(
        uow,
        server.id,
        action=ScheduleAction.STOP,
        warning_steps=(WarningStep(offset_minutes=5, message="soon"),),
    )
    updated = await _update(uow)(
        community_id=_COMMUNITY,
        server_id=server.id,
        schedule_id=seeded.id,
        authorize=_allow,
        name="renamed stop",
    )
    assert len(updated.warning_steps) == 1
    assert updated.name == "renamed stop"


async def test_update_rename_to_existing_name_conflicts() -> None:
    uow, server = _uow()
    _seed_schedule(uow, server.id, name="taken")
    target = _seed_schedule(uow, server.id, name="free")
    with pytest.raises(ScheduleNameAlreadyExistsError):
        await _update(uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            schedule_id=target.id,
            authorize=_allow,
            name="taken",
        )


async def test_update_missing_schedule_is_not_found() -> None:
    uow, server = _uow()
    with pytest.raises(ScheduleNotFoundError):
        await _update(uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            schedule_id=ScheduleId.new(),
            authorize=_allow,
            enabled=False,
        )


async def test_update_switches_cadence_from_interval_to_cron() -> None:
    uow, server = _uow()
    seeded = _seed_schedule(uow, server.id, action=ScheduleAction.START, enabled=True)
    updated = await _update(uow)(
        community_id=_COMMUNITY,
        server_id=server.id,
        schedule_id=seeded.id,
        authorize=_allow,
        cron="0 3 * * *",
    )
    assert updated.cadence.cron == "0 3 * * *"
    assert updated.cadence.interval_seconds is None
    assert updated.next_run_at == dt.datetime(2026, 7, 12, 3, 0, tzinfo=dt.timezone.utc)


# --- warning offset vs interval cadence (issue #1852) -----------------------


async def test_create_rejects_warning_offset_equal_to_interval() -> None:
    # A 10-minute interval with a 10-minute warning offset: the warning instant
    # coincides with the previous occurrence, so it can never fire on time.
    uow, server = _uow()
    with pytest.raises(InvalidSchedulePayloadError):
        await _create(uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            authorize=_allow,
            name="evening stop",
            action=ScheduleAction.STOP,
            interval_seconds=600,
            warning_steps=[(10, "Stopping soon")],
        )
    assert uow.commits == 0


async def test_create_rejects_warning_offset_exceeding_interval() -> None:
    # The issue's example: a 2-minute interval with a 5-minute warning offset.
    uow, server = _uow()
    with pytest.raises(InvalidSchedulePayloadError):
        await _create(uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            authorize=_allow,
            name="evening stop",
            action=ScheduleAction.STOP,
            interval_seconds=120,
            warning_steps=[(5, "Stopping soon")],
        )


async def test_create_accepts_warning_offset_shorter_than_interval() -> None:
    uow, server = _uow()
    schedule = await _create(uow)(
        community_id=_COMMUNITY,
        server_id=server.id,
        authorize=_allow,
        name="evening stop",
        action=ScheduleAction.STOP,
        interval_seconds=600,
        warning_steps=[(9, "Stopping in 9 minutes"), (1, "Stopping now")],
    )
    assert len(schedule.warning_steps) == 2


async def test_create_cron_cadence_skips_warning_offset_check() -> None:
    # Cron cadences have no fixed period, so the offset-vs-interval check does
    # not apply — a two-hour offset on a cron stop schedule is accepted.
    uow, server = _uow()
    schedule = await _create(uow)(
        community_id=_COMMUNITY,
        server_id=server.id,
        authorize=_allow,
        name="nightly stop",
        action=ScheduleAction.STOP,
        cron="0 3 * * *",
        warning_steps=[(120, "Stopping in two hours")],
    )
    assert len(schedule.warning_steps) == 1


async def test_update_rejects_shrinking_interval_below_warning_offset() -> None:
    # Existing stop schedule: 1-hour interval, 10-minute warning. Shrinking the
    # interval to 5 minutes makes the warning unreachable -> rejected.
    uow, server = _uow()
    seeded = _seed_schedule(
        uow,
        server.id,
        action=ScheduleAction.STOP,
        warning_steps=(WarningStep(offset_minutes=10, message="soon"),),
    )
    with pytest.raises(InvalidSchedulePayloadError):
        await _update(uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            schedule_id=seeded.id,
            authorize=_allow,
            interval_seconds=300,
        )


async def test_update_rejects_warning_offset_exceeding_existing_interval() -> None:
    # Existing stop schedule on a 1-hour interval; adding a two-hour warning
    # offset exceeds the interval -> rejected.
    uow, server = _uow()
    seeded = _seed_schedule(uow, server.id, action=ScheduleAction.STOP)
    with pytest.raises(InvalidSchedulePayloadError):
        await _update(uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            schedule_id=seeded.id,
            authorize=_allow,
            warning_steps=[(120, "way too early")],
        )


# --- read / list / delete / runs -------------------------------------------


async def test_read_returns_the_schedule() -> None:
    uow, server = _uow()
    seeded = _seed_schedule(uow, server.id, name="nightly")
    got = await ReadSchedule(uow=uow)(
        community_id=_COMMUNITY, server_id=server.id, schedule_id=seeded.id
    )
    assert got.name == "nightly"


async def test_read_schedule_on_another_server_is_not_found() -> None:
    uow, server = _uow()
    other_server = _server()
    uow.servers.seed(other_server)
    seeded = _seed_schedule(uow, other_server.id, name="elsewhere")
    with pytest.raises(ScheduleNotFoundError):
        await ReadSchedule(uow=uow)(
            community_id=_COMMUNITY, server_id=server.id, schedule_id=seeded.id
        )


async def test_list_returns_server_schedules_ordered_by_name() -> None:
    uow, server = _uow()
    _seed_schedule(uow, server.id, name="beta")
    _seed_schedule(uow, server.id, name="alpha")
    schedules = await ListSchedules(uow=uow)(
        community_id=_COMMUNITY, server_id=server.id
    )
    assert [s.name for s in schedules] == ["alpha", "beta"]


async def test_delete_removes_and_commits() -> None:
    uow, server = _uow()
    seeded = _seed_schedule(uow, server.id, action=ScheduleAction.START)
    await DeleteSchedule(uow=uow)(
        community_id=_COMMUNITY,
        server_id=server.id,
        schedule_id=seeded.id,
        authorize=_allow,
    )
    assert uow.commits == 1
    assert await uow.schedules.get_by_id(seeded.id) is None


async def test_delete_command_schedule_requires_server_command() -> None:
    uow, server = _uow()
    seeded = _seed_schedule(
        uow, server.id, action=ScheduleAction.COMMAND, command="say hi"
    )
    with pytest.raises(PermissionDeniedError) as exc:
        await DeleteSchedule(uow=uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            schedule_id=seeded.id,
            authorize=_authorizer({"schedule:manage"}),
        )
    assert exc.value.permission == "server:command"


async def test_list_runs_returns_history_newest_first() -> None:
    uow, server = _uow()
    seeded = _seed_schedule(uow, server.id)
    older = ScheduleRun(
        id=ScheduleRunId.new(),
        schedule_id=seeded.id,
        started_at=_NOW,
        finished_at=_NOW,
        outcome=ScheduleRunOutcome.SUCCESS,
        detail=None,
    )
    newer = ScheduleRun(
        id=ScheduleRunId.new(),
        schedule_id=seeded.id,
        started_at=_NOW + dt.timedelta(hours=1),
        finished_at=_NOW + dt.timedelta(hours=1),
        outcome=ScheduleRunOutcome.FAILURE,
        detail="boom",
    )
    uow.schedule_runs.seed(older)
    uow.schedule_runs.seed(newer)
    runs = await ListScheduleRuns(uow=uow)(
        community_id=_COMMUNITY, server_id=server.id, schedule_id=seeded.id
    )
    assert [r.id for r in runs] == [newer.id, older.id]


async def test_list_runs_missing_schedule_is_not_found() -> None:
    uow, server = _uow()
    with pytest.raises(ScheduleNotFoundError):
        await ListScheduleRuns(uow=uow)(
            community_id=_COMMUNITY,
            server_id=server.id,
            schedule_id=ScheduleId.new(),
        )
