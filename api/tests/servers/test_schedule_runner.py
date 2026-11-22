"""Use-case tests for the general-scheduler runner (epic #649, issue #1838).

Drives :class:`RunScheduleTick` (wrapping the real :class:`ExecuteScheduleAction`
over the existing lifecycle/command/backup use cases) against in-memory fakes and
a faked clock, covering the outcome taxonomy (success / skipped / failure), the
missed-run semantics (overdue backup coalesces to one catch-up; overdue non-backup
does not fire late), the bounded backup retry, the run-history cap, audit +
notification emission, and per-schedule tick isolation.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field, fields, replace

import pytest

from mc_server_dashboard_api.audit.domain.events import Outcome
from mc_server_dashboard_api.audit.domain.operations import (
    SCHEDULE_RUN,
    TARGET_SCHEDULE,
)
from mc_server_dashboard_api.servers.adapters.cronsim_next_run_calculator import (
    CronsimNextRunCalculator,
)
from mc_server_dashboard_api.servers.application.backups import (
    CreateBackup,
    PruneScheduledBackups,
)
from mc_server_dashboard_api.servers.application.lifecycle import (
    RestartServer,
    SendServerCommand,
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.application.schedule_runner import (
    LATE_RUN_GRACE,
    WARNING_GRACE_FLOOR,
    ActionResult,
    Deferred,
    ExecuteScheduleAction,
    RunScheduleTick,
)
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    SnapshotServer,
)
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    MAX_CONSOLE_LINE_BYTES,
    CommandOutcome,
    CommandStatus,
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import ServerAssignedError
from mc_server_dashboard_api.servers.domain.schedule import (
    Cadence,
    Schedule,
    ScheduleAction,
    ScheduleId,
    ScheduleRun,
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
    WorkerId,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.servers.fakes import (
    DatabaseOutageError,
    FakeBackupArchiveStore,
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeServerNotifier,
    FakeServerRepository,
    FakeStoreGenerationReader,
    FakeUnitOfWork,
    OutageUnitOfWork,
)

_NOW = dt.datetime(2026, 7, 11, 12, 0, 30, tzinfo=dt.timezone.utc)
_HOUR = 3600
_WORKER = WorkerId(uuid.uuid4())
# The stock reconciler.grace_seconds, which bounds a held backup's deferral.
_DEFERRAL_BOUND = dt.timedelta(seconds=660)
_TICK = dt.timedelta(seconds=20)


def _server(
    *,
    desired: DesiredState,
    observed: ObservedState,
    worker: WorkerId | None,
) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=desired,
        observed_state=observed,
        observed_at=None,
        assigned_worker_id=worker,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _running_server() -> Server:
    return _server(
        desired=DesiredState.RUNNING, observed=ObservedState.RUNNING, worker=_WORKER
    )


def _stopped_server() -> Server:
    return _server(
        desired=DesiredState.STOPPED, observed=ObservedState.STOPPED, worker=None
    )


def _schedule(
    server: Server,
    *,
    action: ScheduleAction,
    cadence: Cadence | None = None,
    command: str | None = None,
    warning_steps: tuple[WarningStep, ...] = (),
    only_when_running: bool | None = None,
    next_run_at: dt.datetime,
    last_run_at: dt.datetime | None = None,
    enabled: bool = True,
) -> Schedule:
    return Schedule(
        id=ScheduleId.new(),
        server_id=server.id,
        name="nightly",
        action=action,
        cadence=cadence or Cadence.from_interval(_HOUR),
        enabled=enabled,
        created_at=_NOW,
        updated_at=_NOW,
        command=command,
        warning_steps=warning_steps,
        only_when_running=only_when_running,
        next_run_at=next_run_at,
        last_run_at=last_run_at,
    )


@dataclass
class _Env:
    uow: FakeUnitOfWork
    runner: RunScheduleTick
    notifier: FakeServerNotifier
    audit: RecordingAuditRecorder
    control_plane: FakeControlPlane
    clock: FakeClock


def _env(
    *,
    control_plane: FakeControlPlane | None = None,
    backup_store: FakeBackupArchiveStore | None = None,
    clock: FakeClock | None = None,
    store_generation: FakeStoreGenerationReader | None = None,
    history_cap: int = 50,
    backup_retry_delay: dt.timedelta = dt.timedelta(minutes=30),
    warning_grace: dt.timedelta = WARNING_GRACE_FLOOR,
    deferral_bound: dt.timedelta = _DEFERRAL_BOUND,
    uow: FakeUnitOfWork | None = None,
) -> _Env:
    uow = uow or FakeUnitOfWork()
    cp = control_plane or FakeControlPlane()
    the_clock = clock or FakeClock(_NOW)
    store = backup_store or FakeBackupArchiveStore()

    def make_execute() -> ExecuteScheduleAction:
        return ExecuteScheduleAction(
            uow=uow,
            send_command=SendServerCommand(uow=uow, control_plane=cp),
            start_server=StartServer(
                uow=uow,
                control_plane=cp,
                clock=the_clock,
                jar_provisioner=FakeJarProvisioner(),
                store_generation=store_generation or FakeStoreGenerationReader(),
                # An accepted EULA, so a scheduled start reaches its dispatch.
                file_store=FakeFileStore(seed_eula=True),
            ),
            stop_server=StopServer(uow=uow, control_plane=cp, clock=the_clock),
            restart_server=RestartServer(uow=uow, control_plane=cp, clock=the_clock),
            create_backup=CreateBackup(
                uow=uow,
                backup_store=store,
                snapshot_server=SnapshotServer(uow=uow, control_plane=cp),
                clock=the_clock,
            ),
        )

    notifier = FakeServerNotifier()
    audit = RecordingAuditRecorder()
    runner = RunScheduleTick(
        uow=uow,
        make_execute=make_execute,
        send_command=SendServerCommand(uow=uow, control_plane=cp),
        calculator=CronsimNextRunCalculator(),
        audit=audit,
        notifier=notifier,
        clock=the_clock,
        deferral_bound=deferral_bound,
        history_cap=history_cap,
        backup_retry_delay=backup_retry_delay,
        warning_grace=warning_grace,
        # The retention prune hook (issue #1841): fires after any successful
        # backup-action execution, isolated so its failure never fails the run.
        prune_backups=PruneScheduledBackups(
            uow=uow,
            backup_store=store,
            audit=audit,
            clock=the_clock,
        ),
    )
    return _Env(uow, runner, notifier, audit, cp, the_clock)


def _runs(env: _Env, schedule: Schedule) -> list[ScheduleRunOutcome]:
    return [
        run.outcome
        for run in env.uow.schedule_runs.rows
        if run.schedule_id == schedule.id
    ]


# --- success path ----------------------------------------------------------


async def test_due_schedule_executes_once_records_success_and_advances() -> None:
    env = _env()
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say hi",
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    # Exactly one command dispatch, one success run row, audited, no notification.
    assert [k for k, *_ in env.control_plane.dispatched] == ["command"]
    assert _runs(env, schedule) == [ScheduleRunOutcome.SUCCESS]
    assert env.notifier.notifications == []
    assert len(env.audit.events) == 1
    event = env.audit.events[0]
    assert event.operation == SCHEDULE_RUN
    assert event.outcome is Outcome.SUCCESS
    assert event.actor_id is None
    assert event.target_type == TARGET_SCHEDULE
    assert event.target_id == schedule.id.value
    assert event.community_id == server.community_id.value
    # last/next run persisted; next_run advanced strictly past now.
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.last_run_at == _NOW
    assert stored.next_run_at is not None and stored.next_run_at > _NOW


async def test_cron_schedule_advances_via_calculator() -> None:
    env = _env()
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        cadence=Cadence.from_cron("0 * * * *"),  # top of every hour
        command="say hi",
        next_run_at=_NOW.replace(minute=0, second=0),  # the 12:00 occurrence
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.SUCCESS]
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.next_run_at == _NOW.replace(hour=13, minute=0, second=0)


# --- skip path (precondition unmet) ---------------------------------------


async def test_command_on_stopped_server_records_skipped_no_notification() -> None:
    env = _env()
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say hi",
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    # Nothing dispatched; recorded as skipped; not notified; not audited.
    assert env.control_plane.dispatched == []
    assert _runs(env, schedule) == [ScheduleRunOutcome.SKIPPED]
    assert env.notifier.notifications == []
    assert env.audit.events == []
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.last_run_at == _NOW
    assert stored.next_run_at is not None and stored.next_run_at > _NOW


async def test_start_when_already_running_is_skipped() -> None:
    env = _env()
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.START,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.SKIPPED]
    assert env.notifier.notifications == []


@pytest.mark.parametrize(
    "observed",
    [ObservedState.STARTING, ObservedState.STOPPING, ObservedState.UNKNOWN],
)
async def test_backup_on_transitional_server_is_skipped(
    observed: ObservedState,
) -> None:
    # ``stopping`` and ``unknown`` under a running intent are the shapes a row
    # takes during the failed-stop-orphan window (issues #2471/#2475): the Worker
    # emitted ``stopping`` on entry to the Stop that failed, then reports the
    # orphan as ``unknown`` while its converger works it. The row is honestly
    # diverged for as long as that takes, so a scheduled backup must record a
    # per-run SKIP each tick rather than dispatch into the Worker's refusal and
    # bank a failure — and a skip fires no notification.
    env = _env()
    server = _server(desired=DesiredState.RUNNING, observed=observed, worker=_WORKER)
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert env.control_plane.dispatched == []
    assert _runs(env, schedule) == [ScheduleRunOutcome.SKIPPED]
    assert env.notifier.notifications == []
    assert await env.uow.backups.list_for_server(server.id) == []


async def test_only_when_running_backup_on_stopped_server_is_skipped() -> None:
    # Issue #2236: with the option on (the default), an occurrence that finds
    # the server stopped records a SKIP — no archive, no notification — and
    # next_run_at advances normally.
    env = _env()
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=True,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.SKIPPED]
    assert _run_details(env, schedule) == ["server not running"]
    assert await env.uow.backups.list_for_server(server.id) == []
    assert env.notifier.notifications == []
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.next_run_at is not None and stored.next_run_at > _NOW


async def test_only_when_running_backup_on_running_server_succeeds() -> None:
    env = _env()
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=True,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.SUCCESS]
    assert len(await env.uow.backups.list_for_server(server.id)) == 1


async def test_backup_with_option_off_still_backs_up_a_stopped_server() -> None:
    env = _env()
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.SUCCESS]
    assert len(await env.uow.backups.list_for_server(server.id)) == 1


# --- a backup of a server a Worker still holds (issue #3998) ---------------
#
# A stop holds its Worker until the final snapshot settles. An archive taken then
# would lack that snapshot, so CreateBackup refuses the row ``server_assigned``.
# The occurrence is not a skip and not a failure: it is not recorded, its
# ``next_run_at`` stays, and the next tick runs it again.


def _deferred_lines(caplog: pytest.LogCaptureFixture, schedule: Schedule) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
        and str(schedule.id.value) in record.getMessage()
        and "deferred" in record.getMessage()
    ]


@pytest.mark.parametrize(
    "observed", [ObservedState.STOPPED, ObservedState.CRASHED, ObservedState.UNKNOWN]
)
async def test_a_backup_of_a_server_a_worker_still_holds_waits_for_the_next_tick(
    observed: ObservedState, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    env = _env()
    server = _server(desired=DesiredState.STOPPED, observed=observed, worker=_WORKER)
    due = _NOW - dt.timedelta(seconds=10)
    schedule = _schedule(
        server, action=ScheduleAction.BACKUP, only_when_running=False, next_run_at=due
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == []
    assert env.uow.schedules.by_id[schedule.id].next_run_at == due
    assert await env.uow.backups.list_for_server(server.id) == []
    assert env.notifier.notifications == []
    assert env.audit.events == []
    assert len(_deferred_lines(caplog, schedule)) == 1

    # The final snapshot settles and the stop releases the server: the next tick
    # takes the backup.
    env.uow.servers.by_id[server.id].assigned_worker_id = None
    env.clock.set(_NOW + dt.timedelta(seconds=20))
    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.SUCCESS]
    assert len(await env.uow.backups.list_for_server(server.id)) == 1


@dataclass(frozen=True)
class _StopLandsFirst(CreateBackup):
    """Lands a stop between the runner's read of the row and the backup's own.

    The runner reads the row outside the lifecycle lock, so a stop can make the
    running server it saw read at rest, still assigned, by the time the backup
    reads it.
    """

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        source: BackupSource,
        created_by: uuid.UUID | None = None,
    ) -> Backup:
        assert isinstance(self.uow, FakeUnitOfWork)
        stored = self.uow.servers.by_id[server_id]
        stored.desired_state = DesiredState.STOPPED
        stored.observed_state = ObservedState.STOPPED
        return await super().__call__(
            community_id=community_id,
            server_id=server_id,
            source=source,
            created_by=created_by,
        )


async def test_a_backup_raced_by_a_stop_waits_for_the_next_tick(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    env = _env()
    server = _running_server()
    due = _NOW - dt.timedelta(seconds=10)
    schedule = _schedule(
        server, action=ScheduleAction.BACKUP, only_when_running=False, next_run_at=due
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    base_factory = env.runner.make_execute

    def _make_raced() -> ExecuteScheduleAction:
        base = base_factory()
        return replace(
            base,
            create_backup=_StopLandsFirst(
                **{
                    f.name: getattr(base.create_backup, f.name)
                    for f in fields(base.create_backup)
                }
            ),
        )

    env.runner.make_execute = _make_raced

    await env.runner.tick()

    assert _runs(env, schedule) == []
    assert env.uow.schedules.by_id[schedule.id].next_run_at == due
    assert await env.uow.backups.list_for_server(server.id) == []
    assert env.notifier.notifications == []
    assert env.runner._backup_retry == {}
    assert len(_deferred_lines(caplog, schedule)) == 1


@dataclass(frozen=True)
class _RefusesAssigned(StartServer):
    """A start that answers ``server_assigned``, which only a backup defers."""

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        accept_eula: bool = False,
    ) -> Server:
        raise ServerAssignedError(str(server_id.value))


async def test_only_a_backup_is_deferred_on_server_assigned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Any other action refused ``server_assigned`` is an ordinary failure: it is
    # recorded and advances rather than being owed at every tick (issue #3998).
    caplog.set_level(logging.INFO)
    env = _env()
    server = _stopped_server()
    schedule = _schedule(
        server, action=ScheduleAction.START, next_run_at=_NOW - dt.timedelta(seconds=10)
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    base_factory = env.runner.make_execute

    def _make_refusing() -> ExecuteScheduleAction:
        base = base_factory()
        return replace(
            base,
            start_server=_RefusesAssigned(
                **{
                    f.name: getattr(base.start_server, f.name)
                    for f in fields(base.start_server)
                }
            ),
        )

    env.runner.make_execute = _make_refusing

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.next_run_at is not None and stored.next_run_at > _NOW
    assert _deferred_lines(caplog, schedule) == []


async def test_a_backup_retry_of_a_server_a_worker_still_holds_waits_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    store = FakeBackupArchiveStore(missing=True)  # the occurrence fails
    env = _env(backup_store=store)
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    await env.runner.tick()  # fails -> arms the retry
    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], _NOW + dt.timedelta(days=1)
    )

    # At the retry, the server is at rest but a stop still holds its Worker.
    env.uow.servers.by_id[server.id].assigned_worker_id = _WORKER
    store._missing = False
    env.clock.set(_NOW + dt.timedelta(minutes=30))
    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert len(_deferred_lines(caplog, schedule)) == 1

    # Released: the next tick takes the retry it still owes.
    env.uow.servers.by_id[server.id].assigned_worker_id = None
    env.clock.set(_NOW + dt.timedelta(minutes=30, seconds=20))
    await env.runner.tick()

    assert _runs(env, schedule) == [
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.SUCCESS,
    ]
    assert env.runner._deferred == {}


# The row can stay held for as long as the Worker refuses the snapshot. So the
# deferral is bounded (issue #4047): logged once when it begins, and past the
# bound the backup is recorded as skipped ``server_assigned``. Hourly cron
# cadences keep the next occurrence clear of the ticks below.
_HOURLY = Cadence.from_cron("0 * * * *")


def _rows(env: _Env, schedule: Schedule) -> list[tuple[ScheduleRunOutcome, str | None]]:
    return [
        (run.outcome, run.detail)
        for run in env.uow.schedule_runs.rows
        if run.schedule_id == schedule.id
    ]


def _gave_up_lines(caplog: pytest.LogCaptureFixture, schedule: Schedule) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and str(schedule.id.value) in record.getMessage()
        and "recorded as skipped" in record.getMessage()
    ]


async def _tick_at(env: _Env, at: dt.datetime) -> None:
    env.clock.set(at)
    await env.runner.tick()


def _held_server() -> Server:
    return _server(
        desired=DesiredState.STOPPED, observed=ObservedState.STOPPED, worker=_WORKER
    )


async def test_a_held_backup_is_deferred_up_to_the_bound_then_skipped_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    env = _env()
    server = _held_server()
    due = _NOW.replace(minute=0, second=0)
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        cadence=_HOURLY,
        only_when_running=False,
        next_run_at=due,
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    # Deferred at every tick up to the bound itself, and logged once.
    elapsed = dt.timedelta(0)
    while elapsed <= _DEFERRAL_BOUND:
        await _tick_at(env, _NOW + elapsed)
        elapsed += _TICK
    assert _rows(env, schedule) == []
    assert env.uow.schedules.by_id[schedule.id].next_run_at == due
    assert len(_deferred_lines(caplog, schedule)) == 1
    assert _gave_up_lines(caplog, schedule) == []

    # Past it: one skipped run, and the schedule moves on to its next occurrence.
    await _tick_at(env, _NOW + elapsed)
    assert _rows(env, schedule) == [(ScheduleRunOutcome.SKIPPED, "server_assigned")]
    assert len(_gave_up_lines(caplog, schedule)) == 1
    assert env.runner._deferred == {}
    next_run = due + dt.timedelta(hours=1)
    assert env.uow.schedules.by_id[schedule.id].next_run_at == next_run
    assert await env.uow.backups.list_for_server(server.id) == []
    assert env.notifier.notifications == []

    # That occurrence, still held, gets a whole bound of its own.
    await _tick_at(env, next_run)
    assert len(_rows(env, schedule)) == 1
    assert len(_deferred_lines(caplog, schedule)) == 2

    # Released: it is backed up.
    env.uow.servers.by_id[server.id].assigned_worker_id = None
    await _tick_at(env, next_run + _TICK)
    assert _rows(env, schedule)[1:] == [(ScheduleRunOutcome.SUCCESS, None)]


async def test_a_hold_after_a_released_one_gets_a_whole_bound() -> None:
    env = _env()
    server = _held_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        cadence=_HOURLY,
        only_when_running=False,
        next_run_at=_NOW.replace(minute=0, second=0),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    await _tick_at(env, _NOW)  # deferred
    env.uow.servers.by_id[server.id].assigned_worker_id = None
    await _tick_at(env, _NOW + _TICK)
    assert _runs(env, schedule) == [ScheduleRunOutcome.SUCCESS]
    assert env.runner._deferred == {}

    # The next occurrence is due more than a bound after the first deferral, and
    # a stop holds the row again: the deferral is timed from now, not from then.
    next_run = env.uow.schedules.by_id[schedule.id].next_run_at
    assert next_run is not None and next_run - _NOW > _DEFERRAL_BOUND
    env.uow.servers.by_id[server.id].assigned_worker_id = _WORKER
    await _tick_at(env, next_run)

    assert _runs(env, schedule) == [ScheduleRunOutcome.SUCCESS]
    assert env.uow.schedules.by_id[schedule.id].next_run_at == next_run


async def test_a_held_occurrence_an_edit_moves_on_gets_a_whole_bound() -> None:
    env = _env()
    server = _held_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        cadence=_HOURLY,
        only_when_running=False,
        next_run_at=_NOW.replace(minute=0, second=0),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    await _tick_at(env, _NOW)  # deferred

    # An edit moves the occurrence on while a stop still holds the row: the new
    # one, due more than a bound after the first deferral, is deferred afresh.
    later = _NOW + 2 * _DEFERRAL_BOUND
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], later
    )
    await _tick_at(env, later)

    assert _rows(env, schedule) == []
    assert env.uow.schedules.by_id[schedule.id].next_run_at == later


async def test_a_held_retry_is_bounded_too_and_leaves_next_run_at(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    bound = dt.timedelta(seconds=60)
    store = FakeBackupArchiveStore(missing=True)  # the occurrence fails
    env = _env(backup_store=store, deferral_bound=bound)
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        cadence=_HOURLY,
        only_when_running=False,
        next_run_at=_NOW.replace(minute=0, second=0),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    await env.runner.tick()  # fails -> arms the retry 30 minutes on
    # Far enough out that an advance would show.
    next_run = _NOW + dt.timedelta(days=1)
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], next_run
    )

    # At the retry a stop holds the row, for longer than the bound.
    env.uow.servers.by_id[server.id].assigned_worker_id = _WORKER
    store._missing = False
    retry_at = _NOW + dt.timedelta(minutes=30)
    await _tick_at(env, retry_at)
    await _tick_at(env, retry_at + bound)
    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    await _tick_at(env, retry_at + bound + _TICK)

    # The retry is spent as one skipped run, and never moves next_run_at.
    assert _rows(env, schedule)[1:] == [(ScheduleRunOutcome.SKIPPED, "server_assigned")]
    assert env.uow.schedules.by_id[schedule.id].next_run_at == next_run
    assert env.runner._backup_retry == {}
    assert len(_deferred_lines(caplog, schedule)) == 1


async def test_a_held_occurrence_with_a_retry_pending_is_skipped_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    store = FakeBackupArchiveStore(missing=True)  # the occurrence fails
    env = _env(backup_store=store)
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        cadence=_HOURLY,
        only_when_running=False,
        next_run_at=_NOW.replace(minute=0, second=0),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    await env.runner.tick()  # fails -> arms the retry 30 minutes on

    # An occurrence comes due with the retry, on a row a stop now holds, and the
    # hold outlasts two bounds.
    retry_at = _NOW + dt.timedelta(minutes=30)
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], retry_at
    )
    env.uow.servers.by_id[server.id].assigned_worker_id = _WORKER
    store._missing = False
    elapsed = dt.timedelta(0)
    while elapsed <= 2 * _DEFERRAL_BOUND + _TICK:
        await _tick_at(env, retry_at + elapsed)
        elapsed += _TICK

    # One skipped run stands for the backup the schedule owes. The retry would
    # meet the same hold, so it goes with the occurrence rather than being
    # deferred and skipped a second time.
    assert _runs(env, schedule) == [
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.SKIPPED,
    ]
    assert env.runner._backup_retry == {}
    assert len(_deferred_lines(caplog, schedule)) == 1


async def test_a_held_occurrence_skip_keeps_a_retry_not_yet_due() -> None:
    # 03:00 fails and arms the retry for 03:30. 03:10 is held past the bound and
    # skipped at about 03:21, and the hold ends at 03:25: the retry the 03:00
    # failure is owed still runs, though the next occurrence is a day away.
    day = dt.datetime(2026, 7, 11, tzinfo=dt.timezone.utc)
    store = FakeBackupArchiveStore(missing=True)  # the 03:00 occurrence fails
    env = _env(backup_store=store, clock=FakeClock(day.replace(hour=3)))
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        cadence=Cadence.from_cron("0,10 3 * * *"),
        only_when_running=False,
        next_run_at=day.replace(hour=3),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    await env.runner.tick()
    assert env.runner._backup_retry == {schedule.id: day.replace(hour=3, minute=30)}

    env.uow.servers.by_id[server.id].assigned_worker_id = _WORKER
    store._missing = False
    held_at = day.replace(hour=3, minute=10)
    await _tick_at(env, held_at)
    await _tick_at(env, held_at + _DEFERRAL_BOUND + _TICK)
    assert _runs(env, schedule) == [
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.SKIPPED,
    ]
    assert env.runner._backup_retry == {schedule.id: day.replace(hour=3, minute=30)}

    env.uow.servers.by_id[server.id].assigned_worker_id = None  # released at 03:25
    await _tick_at(env, day.replace(hour=3, minute=30))

    assert _runs(env, schedule) == [
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.SKIPPED,
        ScheduleRunOutcome.SUCCESS,
    ]
    assert env.runner._backup_retry == {}
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.next_run_at == day.replace(day=12, hour=3)


async def test_a_held_occurrence_skip_takes_a_retry_due_that_very_tick() -> None:
    store = FakeBackupArchiveStore(missing=True)  # the occurrence fails
    env = _env(backup_store=store)
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        cadence=_HOURLY,
        only_when_running=False,
        next_run_at=_NOW.replace(minute=0, second=0),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    await env.runner.tick()  # fails -> arms the retry 30 minutes on
    retry_at = _NOW + dt.timedelta(minutes=30)
    assert env.runner._backup_retry == {schedule.id: retry_at}

    # An occurrence first deferred a bound and a tick before the retry is skipped
    # at the retry's own instant. The retry is due then, so it goes with it.
    first = retry_at - _DEFERRAL_BOUND - _TICK
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], first
    )
    env.uow.servers.by_id[server.id].assigned_worker_id = _WORKER
    store._missing = False
    at = first
    while at <= retry_at:
        await _tick_at(env, at)
        at += _TICK

    assert _runs(env, schedule) == [
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.SKIPPED,
    ]
    assert env.runner._backup_retry == {}


def _fail_one_record(env: _Env) -> None:
    real_add = env.uow.schedule_runs.add
    failures = [RuntimeError("database down")]

    async def failing_add(run: ScheduleRun) -> None:
        if failures:
            raise failures.pop()
        await real_add(run)

    env.uow.schedule_runs.add = failing_add  # type: ignore[method-assign]


async def test_a_held_retry_whose_skip_fails_to_record_is_recorded_next_tick(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    bound = dt.timedelta(seconds=60)
    store = FakeBackupArchiveStore(missing=True)  # the occurrence fails
    env = _env(backup_store=store, deferral_bound=bound)
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        cadence=_HOURLY,
        only_when_running=False,
        next_run_at=_NOW.replace(minute=0, second=0),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    await env.runner.tick()  # fails -> arms the retry 30 minutes on
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], _NOW + dt.timedelta(days=1)
    )
    env.uow.servers.by_id[server.id].assigned_worker_id = _WORKER
    store._missing = False
    retry_at = _NOW + dt.timedelta(minutes=30)
    await _tick_at(env, retry_at)  # deferred

    # The retry's skipped run fails to record once: the retry stays armed, and
    # the next tick records it without opening a second window.
    _fail_one_record(env)
    past = retry_at + bound + _TICK
    await _tick_at(env, past)
    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    await _tick_at(env, past + _TICK)

    assert _rows(env, schedule)[1:] == [(ScheduleRunOutcome.SKIPPED, "server_assigned")]
    assert len(_deferred_lines(caplog, schedule)) == 1
    assert len(_gave_up_lines(caplog, schedule)) == 1
    assert env.runner._backup_retry == {}
    assert env.runner._deferred == {}


async def test_a_held_backup_given_up_as_its_schedule_goes_is_not_said_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    env = _env()
    server = _held_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        cadence=_HOURLY,
        only_when_running=False,
        next_run_at=_NOW.replace(minute=0, second=0),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    await _tick_at(env, _NOW)  # deferred

    # Past the bound, the schedule is deleted after its claim and before the
    # skipped run's record.
    real_lock = env.uow.schedules.lock

    async def lock_then_delete(schedule_id: ScheduleId) -> Schedule | None:
        found = await real_lock(schedule_id)
        env.uow.schedules.by_id.pop(schedule_id, None)
        return found

    env.uow.schedules.lock = lock_then_delete  # type: ignore[method-assign]
    await _tick_at(env, _NOW + _DEFERRAL_BOUND + _TICK)

    assert _rows(env, schedule) == []
    assert _gave_up_lines(caplog, schedule) == []
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and str(schedule.id.value) in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "the schedule was deleted" in warnings[0]
    assert env.runner._deferred == {}


async def test_a_held_retry_given_up_as_its_schedule_goes_is_not_said_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    store = FakeBackupArchiveStore(missing=True)  # the occurrence fails
    env = _env(backup_store=store)
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        cadence=_HOURLY,
        only_when_running=False,
        next_run_at=_NOW.replace(minute=0, second=0),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    await env.runner.tick()  # fails -> arms the retry 30 minutes on
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], _NOW + dt.timedelta(days=1)
    )
    env.uow.servers.by_id[server.id].assigned_worker_id = _WORKER
    store._missing = False
    retry_at = _NOW + dt.timedelta(minutes=30)
    await _tick_at(env, retry_at)  # deferred

    # Past the bound, the schedule is deleted after the retry's claim and before
    # its skipped run's record.
    real_lock = env.uow.schedules.lock

    async def lock_then_delete(schedule_id: ScheduleId) -> Schedule | None:
        found = await real_lock(schedule_id)
        env.uow.schedules.by_id.pop(schedule_id, None)
        return found

    env.uow.schedules.lock = lock_then_delete  # type: ignore[method-assign]
    await _tick_at(env, retry_at + _DEFERRAL_BOUND + _TICK)

    assert _rows(env, schedule) == [(ScheduleRunOutcome.FAILURE, "action failed")]
    assert _gave_up_lines(caplog, schedule) == []
    warnings = [
        line
        for line in _lines_naming(caplog, logging.WARNING, schedule)
        if "backup retry" in line
    ]
    assert len(warnings) == 1
    assert "the schedule was deleted" in warnings[0]
    assert env.runner._backup_retry == {}
    assert env.runner._deferred == {}


async def test_a_deferral_outlives_neither_its_schedule_nor_a_failed_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    env = _env()
    server = _held_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        cadence=_HOURLY,
        only_when_running=False,
        next_run_at=_NOW.replace(minute=0, second=0),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    await _tick_at(env, _NOW)  # deferred

    # The skipped run's record fails once: the deferral stands, still past the
    # bound, and the next tick records it without opening a second window.
    _fail_one_record(env)
    past = _NOW + _DEFERRAL_BOUND + _TICK
    await _tick_at(env, past)
    assert _rows(env, schedule) == []
    await _tick_at(env, past + _TICK)
    assert _rows(env, schedule) == [(ScheduleRunOutcome.SKIPPED, "server_assigned")]
    assert len(_deferred_lines(caplog, schedule)) == 1
    assert len(_gave_up_lines(caplog, schedule)) == 1

    # A schedule deleted between ticks while deferred is swept from the record.
    other = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        cadence=_HOURLY,
        only_when_running=False,
        next_run_at=past,
    )
    env.uow.schedules.seed(other)
    await _tick_at(env, past + 2 * _TICK)
    assert other.id in env.runner._deferred
    del env.uow.schedules.by_id[other.id]
    await _tick_at(env, past + 3 * _TICK)
    assert env.runner._deferred == {}


# --- failure path ----------------------------------------------------------


async def test_dispatch_failure_records_failure_notifies_and_advances() -> None:
    refuse = FakeControlPlane(
        outcomes={"command": CommandOutcome(status=CommandStatus.INTERNAL)}
    )
    env = _env(control_plane=refuse)
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say hi",
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    # One NOTIFICATION frame, scoped to the server, with the failure discriminator.
    assert len(env.notifier.notifications) == 1
    server_id, kind, title, _detail = env.notifier.notifications[0]
    assert server_id == server.id
    assert kind == "schedule_failed"
    assert "command" in title
    # Audited as an error; next-run still advances (no tick-level retry).
    assert [e.outcome for e in env.audit.events] == [Outcome.ERROR]
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.next_run_at is not None and stored.next_run_at > _NOW


async def test_sustained_failures_notify_once_then_recover() -> None:
    # State-transition suppression (#2269): a schedule stuck failing every
    # occurrence emits exactly one failure toast for the whole incident, then one
    # recovery toast when it clears. The run rows are still recorded every time.
    refuse = FakeControlPlane(
        outcomes={"command": CommandOutcome(status=CommandStatus.INTERNAL)}
    )
    env = _env(control_plane=refuse)
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say hi",
        cadence=Cadence.from_interval(60),
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # occurrence 1: fails
    env.clock.set(_NOW + dt.timedelta(minutes=1))
    await env.runner.tick()  # occurrence 2: fails again

    # Both occurrences recorded, but only the first notifies (no dedup today: 2).
    assert _runs(env, schedule) == [
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.FAILURE,
    ]
    assert len(env.notifier.notifications) == 1

    refuse._outcomes["command"] = CommandOutcome(status=CommandStatus.OK)
    env.clock.set(_NOW + dt.timedelta(minutes=2))
    await env.runner.tick()  # recovers

    assert len(env.notifier.notifications) == 2
    server_id, kind, title, _detail = env.notifier.notifications[1]
    assert server_id == server.id
    assert kind == "schedule_recovered"
    assert "command" in title


class _DiskFullBackupStore(FakeBackupArchiveStore):
    """Raises a non-ServerError mid-archive (the disk-full case)."""

    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        raise OSError("disk full")


@dataclass(frozen=True)
class _SpyPrune(PruneScheduledBackups):
    """Records invocations without pruning, so a test can assert (non-)calls."""

    calls: list[tuple[CommunityId, ServerId]] = field(default_factory=list)

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> list[Backup]:
        self.calls.append((community_id, server_id))
        return []


async def test_unexpected_exception_is_classified_as_failure() -> None:
    env = _env(backup_store=_DiskFullBackupStore())
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    # A non-ServerError still lands in the taxonomy: run row + audit +
    # notification, and next_run advances — no every-tick re-execution.
    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert [e.outcome for e in env.audit.events] == [Outcome.ERROR]
    assert len(env.notifier.notifications) == 1
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.next_run_at is not None and stored.next_run_at > _NOW


def _run_details(env: _Env, schedule: Schedule) -> list[str | None]:
    return [
        run.detail
        for run in env.uow.schedule_runs.rows
        if run.schedule_id == schedule.id
    ]


class _StorageTimeoutBackupStore(FakeBackupArchiveStore):
    """Raises a botocore read timeout mid-archive (the storage-timeout case)."""

    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        from botocore.exceptions import ReadTimeoutError

        raise ReadTimeoutError(endpoint_url="http://seaweedfs:8333/bucket/key")


class _StorageInternalErrorBackupStore(FakeBackupArchiveStore):
    """Raises a botocore ClientError 500 mid-archive (the SeaweedFS disk-full case)."""

    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        from botocore.exceptions import ClientError

        raise ClientError(
            {"Error": {"Code": "InternalError", "Message": "s3://bucket/secret-key"}},
            "UploadPart",
        )


class _CorruptBackupStore(FakeBackupArchiveStore):
    """Raises BackupCorruptError mid-archive (the corrupt-working-set refusal)."""

    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        from mc_server_dashboard_api.servers.domain.errors import BackupCorruptError

        raise BackupCorruptError(str(server_id.value), corrupt_count=2)


class _TooLargeBackupStore(FakeBackupArchiveStore):
    """Raises BackupTooLargeError at create (a working set too large to archive)."""

    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        from mc_server_dashboard_api.servers.domain.errors import BackupTooLargeError

        raise BackupTooLargeError(str(server_id.value))


class _StorageUnavailableBackupStore(FakeBackupArchiveStore):
    """Raises BackupStorageUnavailableError mid-archive (the storage-outage case)."""

    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        from mc_server_dashboard_api.servers.domain.errors import (
            BackupStorageUnavailableError,
        )

        raise BackupStorageUnavailableError(str(server_id.value))


class _FailsAndTakesTheDatabaseDown(FakeBackupArchiveStore):
    """The store refuses every archive, and the database goes down with it."""

    def __init__(self, uow: OutageUnitOfWork) -> None:
        super().__init__()
        self._uow = uow
        self.attempts = 0

    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        from mc_server_dashboard_api.servers.domain.errors import (
            BackupStorageUnavailableError,
        )

        self.attempts += 1
        self._uow.down = True
        raise BackupStorageUnavailableError(str(server_id.value))


async def test_backups_failing_in_a_database_outage_leave_no_rows() -> None:
    # Production, 2026-10-15 (issue #4086): the store refused every archive and
    # the database went down with it. No run could be recorded, so the occurrence
    # ran again every tick, and each run left a row with no archive behind.
    uow = OutageUnitOfWork()
    store = _FailsAndTakesTheDatabaseDown(uow)
    env = _env(backup_store=store, uow=uow)
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        # Keeps the next occurrence clear of the ticks below.
        cadence=_HOURLY,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    for tick in range(3):
        uow.down = False
        # The tick's own warnings poll meets the same outage.
        with contextlib.suppress(DatabaseOutageError):
            await _tick_at(env, _NOW + tick * _TICK)

    # It runs once now: a later tick records the run it owes (issue #4108).
    assert store.attempts == 1
    assert env.uow.backups.by_id == {}


# --- an occurrence whose record fails (issue #4108) --------------------------
#
# A backup has no staleness cut-off, so an occurrence whose record and advance
# kept failing ran again every tick, a new archive each time: 21 of them in the
# 2026-10-15 outage. It now runs once, and each later tick retries only its
# record, with the outcome and times of the run that took place.


class _RecordsFail:
    """Makes every run record raise while ``down`` is set, as a database outage."""

    def __init__(self, env: _Env) -> None:
        self.down = True
        real_add = env.uow.schedule_runs.add

        async def add(run: ScheduleRun) -> None:
            if self.down:
                raise RuntimeError("database down")
            await real_add(run)

        env.uow.schedule_runs.add = add  # type: ignore[method-assign]


def _archive_attempts(store: FakeBackupArchiveStore) -> int:
    return sum(1 for _server_id, op in store.events if op == "create-archive")


def _run_rows(
    env: _Env, schedule: Schedule
) -> list[tuple[ScheduleRunOutcome, str | None, dt.datetime, dt.datetime]]:
    return [
        (run.outcome, run.detail, run.started_at, run.finished_at)
        for run in env.uow.schedule_runs.rows
        if run.schedule_id == schedule.id
    ]


def _lines_naming(
    caplog: pytest.LogCaptureFixture, level: int, schedule: Schedule
) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == level and str(schedule.id.value) in record.getMessage()
    ]


def _restart(env: _Env) -> None:
    """Replace the runner as an API restart does: the same wiring, no memory."""

    env.runner = RunScheduleTick(
        **{
            f.name: getattr(env.runner, f.name)
            for f in fields(env.runner)
            if f.init and not f.name.startswith("_")
        }
    )


def _backup_schedule(env: _Env) -> Schedule:
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        cadence=_HOURLY,
        only_when_running=False,
        next_run_at=_NOW.replace(minute=0, second=0),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    return schedule


async def test_an_occurrence_whose_record_fails_runs_once_until_it_is_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    store = FakeBackupArchiveStore()
    env = _env(backup_store=store)
    schedule = _backup_schedule(env)
    outage = _RecordsFail(env)

    for tick in range(5):
        await _tick_at(env, _NOW + tick * _TICK)
    assert _archive_attempts(store) == 1
    assert _run_rows(env, schedule) == []

    outage.down = False
    await _tick_at(env, _NOW + 5 * _TICK)

    assert _archive_attempts(store) == 1
    assert _run_rows(env, schedule) == [(ScheduleRunOutcome.SUCCESS, None, _NOW, _NOW)]
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.next_run_at == _NOW.replace(hour=13, minute=0, second=0)
    assert stored.last_run_at == _NOW
    assert len(_lines_naming(caplog, logging.WARNING, schedule)) == 1
    assert [
        line for line in _lines_naming(caplog, logging.INFO, schedule) if "owed" in line
    ] == [f"schedule {schedule.id.value}: the owed run of its backup is recorded"]


async def test_a_failed_occurrence_whose_record_fails_records_one_failure() -> None:
    store = FakeBackupArchiveStore(missing=True)
    env = _env(backup_store=store)
    schedule = _backup_schedule(env)
    outage = _RecordsFail(env)

    for tick in range(5):
        await _tick_at(env, _NOW + tick * _TICK)
    outage.down = False
    await _tick_at(env, _NOW + 5 * _TICK)

    assert _archive_attempts(store) == 1
    assert _run_rows(env, schedule) == [
        (ScheduleRunOutcome.FAILURE, "action failed", _NOW, _NOW)
    ]
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.next_run_at == _NOW.replace(hour=13, minute=0, second=0)
    assert _titles(env) == ["Scheduled backup failed"]
    assert len(env.audit.events) == 1


async def test_a_restart_runs_an_occurrence_whose_record_failed_once_more() -> None:
    store = FakeBackupArchiveStore()
    env = _env(backup_store=store)
    schedule = _backup_schedule(env)
    outage = _RecordsFail(env)
    for tick in range(3):
        await _tick_at(env, _NOW + tick * _TICK)

    # The restart loses the owed record, so the occurrence, still due, runs once
    # more: and only once, however long the record keeps failing.
    _restart(env)
    restarted_at = _NOW + 3 * _TICK
    for tick in range(3):
        await _tick_at(env, restarted_at + tick * _TICK)
    outage.down = False
    await _tick_at(env, restarted_at + 3 * _TICK)

    assert _archive_attempts(store) == 2
    assert _run_rows(env, schedule) == [
        (ScheduleRunOutcome.SUCCESS, None, restarted_at, restarted_at)
    ]


async def test_a_schedule_owing_a_record_runs_no_backup_retry_either() -> None:
    store = FakeBackupArchiveStore(missing=True)
    env = _env(backup_store=store)
    schedule = _backup_schedule(env)
    await _tick_at(env, _NOW)  # fails -> arms the retry 30 minutes on
    # The next occurrence comes due before the retry, and runs while the
    # database is down; an edit then moves the schedule's next run out.
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], _NOW + dt.timedelta(minutes=10)
    )
    store._missing = False
    outage = _RecordsFail(env)
    await _tick_at(env, _NOW + dt.timedelta(minutes=10))
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], _NOW + dt.timedelta(days=1)
    )

    retry_at = _NOW + dt.timedelta(minutes=30)
    await _tick_at(env, retry_at)
    assert _archive_attempts(store) == 2

    # The owed success is recorded, and spends the retry it made moot.
    outage.down = False
    await _tick_at(env, retry_at + _TICK)
    assert _archive_attempts(store) == 2
    assert _runs(env, schedule) == [
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.SUCCESS,
    ]


async def test_an_owed_record_of_a_deleted_schedule_is_audited_once_and_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    env = _env()
    schedule = _backup_schedule(env)
    outage = _RecordsFail(env)
    await _tick_at(env, _NOW)
    await env.uow.schedules.delete(schedule.id)

    outage.down = False
    await _tick_at(env, _NOW + _TICK)
    await _tick_at(env, _NOW + 2 * _TICK)

    assert _run_rows(env, schedule) == []
    assert [event.target_id for event in env.audit.events] == [schedule.id.value]
    infos = _lines_naming(caplog, logging.INFO, schedule)
    assert (
        f"schedule {schedule.id.value} was deleted during its run; run not recorded"
        in infos
    )
    assert not [line for line in infos if "owed" in line]


async def test_backup_storage_timeout_records_distinguishable_detail() -> None:
    env = _env(backup_store=_StorageTimeoutBackupStore())
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert _run_details(env, schedule) == ["storage timeout"]


async def test_backup_storage_internal_error_records_sanitized_detail() -> None:
    env = _env(backup_store=_StorageInternalErrorBackupStore())
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    detail = _run_details(env, schedule)[0]
    # A distinguishable storage-backend category, not the generic fallback, and
    # never the raw endpoint/key that the botocore message carries.
    assert detail == "storage error"
    assert "bucket" not in (detail or "")
    assert "secret-key" not in (detail or "")


async def test_corrupt_backup_records_backup_corrupt_detail() -> None:
    env = _env(backup_store=_CorruptBackupStore())
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert _run_details(env, schedule) == ["backup corrupt"]


async def test_too_large_backup_records_its_detail_and_is_not_retried() -> None:
    # A working set too large to archive (issue #3859) is as deterministic as a
    # corrupt one: re-archiving it can never succeed, so no retry is armed and no
    # second run row follows the retry delay.
    env = _env(backup_store=_TooLargeBackupStore())
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], _NOW + dt.timedelta(days=1)
    )
    # Past the retry delay, as in the corrupt case below.
    env.clock.set(_NOW + dt.timedelta(minutes=30))
    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert _run_details(env, schedule) == ["backup too large"]


async def test_storage_unavailable_records_distinct_detail() -> None:
    # A typed BackupStorageUnavailableError (#2270) is a distinct failure class,
    # not the generic "action failed" fallback: the run row names the storage
    # outage so the operator toast/audit surfaces *what* failed (#2248/#2274).
    env = _env(backup_store=_StorageUnavailableBackupStore())
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert _run_details(env, schedule) == ["storage unavailable"]


async def test_advance_does_not_resurrect_a_concurrently_disabled_schedule() -> None:
    env = _env()
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say hi",
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    base_factory = env.runner.make_execute

    class _DisableDuringRun(ExecuteScheduleAction):
        """Simulates a CRUD disable landing while the action executes."""

        async def __call__(
            self,
            *,
            server_id: ServerId,
            action: ScheduleAction,
            command: str | None,
            only_when_running: bool | None,
        ) -> ActionResult | Deferred:
            env.uow.schedules.by_id[schedule.id] = replace(
                env.uow.schedules.by_id[schedule.id],
                enabled=False,
                next_run_at=None,
            )
            return await ExecuteScheduleAction.__call__(
                self,
                server_id=server_id,
                action=action,
                command=command,
                only_when_running=only_when_running,
            )

    def _make_disabling() -> ExecuteScheduleAction:
        base = base_factory()
        return _DisableDuringRun(
            uow=base.uow,
            send_command=base.send_command,
            start_server=base.start_server,
            stop_server=base.stop_server,
            restart_server=base.restart_server,
            create_backup=base.create_backup,
        )

    env.runner.make_execute = _make_disabling

    await env.runner.tick()

    # The run itself completed and is recorded, but the bookkeeping advance
    # matched no enabled row: the schedule stays disabled, next_run_at NULL.
    assert _runs(env, schedule) == [ScheduleRunOutcome.SUCCESS]
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.enabled is False
    assert stored.next_run_at is None


# --- missed-run semantics --------------------------------------------------


async def test_overdue_backup_fires_exactly_one_catchup() -> None:
    env = _env()
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=2 * _HOUR),  # two periods overdue
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    # Coalesced: exactly one catch-up backup despite multiple missed occurrences.
    assert _runs(env, schedule) == [ScheduleRunOutcome.SUCCESS]
    assert len(await env.uow.backups.list_for_server(server.id)) == 1
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.next_run_at is not None and stored.next_run_at > _NOW


async def test_overdue_lifecycle_schedule_does_not_fire_late(
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = _env()
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.STOP,
        next_run_at=_NOW - dt.timedelta(seconds=2 * _HOUR),  # hours past the grace
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    # No execution; SKIPPED run row recorded; next_run advanced past now;
    # last_run untouched; warning logged.
    assert env.control_plane.dispatched == []
    assert _runs(env, schedule) == [ScheduleRunOutcome.SKIPPED]
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.last_run_at is None
    assert stored.next_run_at is not None and stored.next_run_at > _NOW
    stale_logs = [r for r in caplog.records if "too stale" in r.message]
    assert len(stale_logs) == 0  # "too stale" is in the run detail, not the log
    skip_logs = [r for r in caplog.records if "skipping" in r.message]
    assert len(skip_logs) == 1


async def test_lifecycle_schedule_at_the_grace_boundary_still_fires() -> None:
    env = _env()
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say hi",
        next_run_at=_NOW - LATE_RUN_GRACE,  # exactly at the staleness boundary
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert [k for k, *_ in env.control_plane.dispatched] == ["command"]
    assert _runs(env, schedule) == [ScheduleRunOutcome.SUCCESS]


async def test_lifecycle_schedule_just_past_the_grace_does_not_fire() -> None:
    env = _env()
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say hi",
        next_run_at=_NOW - LATE_RUN_GRACE - dt.timedelta(seconds=1),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert env.control_plane.dispatched == []
    assert _runs(env, schedule) == [ScheduleRunOutcome.SKIPPED]
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.next_run_at is not None and stored.next_run_at > _NOW


# --- concurrent dispatch (issue #1947) --------------------------------------


async def test_two_due_schedules_on_different_servers_run_concurrently() -> None:
    """Schedules on distinct servers are dispatched concurrently."""
    env = _env()
    server_a = _running_server()
    server_b = _running_server()
    sched_a = _schedule(
        server_a,
        action=ScheduleAction.COMMAND,
        command="say a",
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    sched_b = _schedule(
        server_b,
        action=ScheduleAction.COMMAND,
        command="say b",
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server_a)
    env.uow.servers.seed(server_b)
    env.uow.schedules.seed(sched_a)
    env.uow.schedules.seed(sched_b)

    # Track concurrency via an event: each execution sets a flag before
    # yielding and checks whether the other is also in flight.
    in_flight: set[ServerId] = set()
    max_concurrent = 0
    original_factory = env.runner.make_execute

    def _tracking_factory() -> ExecuteScheduleAction:
        base = original_factory()

        class _Track(ExecuteScheduleAction):
            async def __call__(
                self,
                *,
                server_id: ServerId,
                action: ScheduleAction,
                command: str | None,
                only_when_running: bool | None,
            ) -> ActionResult | Deferred:
                nonlocal max_concurrent
                in_flight.add(server_id)
                max_concurrent = max(max_concurrent, len(in_flight))
                await asyncio.sleep(0)  # yield to allow true concurrency
                result = await ExecuteScheduleAction.__call__(
                    self,
                    server_id=server_id,
                    action=action,
                    command=command,
                    only_when_running=only_when_running,
                )
                in_flight.discard(server_id)
                return result

        return _Track(
            uow=base.uow,
            send_command=base.send_command,
            start_server=base.start_server,
            stop_server=base.stop_server,
            restart_server=base.restart_server,
            create_backup=base.create_backup,
        )

    env.runner.make_execute = _tracking_factory

    await env.runner.tick()

    assert max_concurrent == 2
    assert _runs(env, sched_a) == [ScheduleRunOutcome.SUCCESS]
    assert _runs(env, sched_b) == [ScheduleRunOutcome.SUCCESS]


async def test_same_server_schedules_run_serially() -> None:
    """Two schedules on the same server are executed one after the other."""
    env = _env()
    server = _running_server()
    sched_a = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say a",
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    sched_b = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say b",
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(sched_a)
    env.uow.schedules.seed(sched_b)

    in_flight_count: list[int] = []
    in_flight: set[ScheduleId] = set()
    original_factory = env.runner.make_execute

    def _tracking_factory() -> ExecuteScheduleAction:
        base = original_factory()

        class _Track(ExecuteScheduleAction):
            async def __call__(
                self,
                *,
                server_id: ServerId,
                action: ScheduleAction,
                command: str | None,
                only_when_running: bool | None,
            ) -> ActionResult | Deferred:
                # Use command line as schedule identifier since we can't get
                # schedule id here.
                key = ScheduleId(uuid.uuid4())  # unique per call
                in_flight.add(key)
                in_flight_count.append(len(in_flight))
                await asyncio.sleep(0)
                result = await ExecuteScheduleAction.__call__(
                    self,
                    server_id=server_id,
                    action=action,
                    command=command,
                    only_when_running=only_when_running,
                )
                in_flight.discard(key)
                return result

        return _Track(
            uow=base.uow,
            send_command=base.send_command,
            start_server=base.start_server,
            stop_server=base.stop_server,
            restart_server=base.restart_server,
            create_backup=base.create_backup,
        )

    env.runner.make_execute = _tracking_factory

    await env.runner.tick()

    # Both ran, but never more than 1 in flight at a time for the same server.
    assert len(in_flight_count) == 2
    assert max(in_flight_count) == 1
    assert _runs(env, sched_a) == [ScheduleRunOutcome.SUCCESS]
    assert _runs(env, sched_b) == [ScheduleRunOutcome.SUCCESS]


async def test_concurrent_cap_limits_parallel_executions() -> None:
    """With 5 servers due, at most _MAX_CONCURRENT_RUNS (4) run in parallel."""
    from mc_server_dashboard_api.servers.application.schedule_runner import (
        _MAX_CONCURRENT_RUNS,
    )

    env = _env()
    servers = [_running_server() for _ in range(5)]
    schedules = []
    for srv in servers:
        sched = _schedule(
            srv,
            action=ScheduleAction.COMMAND,
            command="say x",
            next_run_at=_NOW - dt.timedelta(seconds=10),
        )
        env.uow.servers.seed(srv)
        env.uow.schedules.seed(sched)
        schedules.append(sched)

    max_concurrent = 0
    in_flight = 0
    original_factory = env.runner.make_execute

    def _tracking_factory() -> ExecuteScheduleAction:
        base = original_factory()

        class _Track(ExecuteScheduleAction):
            async def __call__(
                self,
                *,
                server_id: ServerId,
                action: ScheduleAction,
                command: str | None,
                only_when_running: bool | None,
            ) -> ActionResult | Deferred:
                nonlocal max_concurrent, in_flight
                in_flight += 1
                max_concurrent = max(max_concurrent, in_flight)
                await asyncio.sleep(0)  # yield to let all allowed tasks enter
                result = await ExecuteScheduleAction.__call__(
                    self,
                    server_id=server_id,
                    action=action,
                    command=command,
                    only_when_running=only_when_running,
                )
                in_flight -= 1
                return result

        return _Track(
            uow=base.uow,
            send_command=base.send_command,
            start_server=base.start_server,
            stop_server=base.stop_server,
            restart_server=base.restart_server,
            create_backup=base.create_backup,
        )

    env.runner.make_execute = _tracking_factory

    await env.runner.tick()

    assert max_concurrent <= _MAX_CONCURRENT_RUNS
    for sched in schedules:
        assert _runs(env, sched) == [ScheduleRunOutcome.SUCCESS]


async def test_exception_in_one_server_group_does_not_stop_others() -> None:
    """Under concurrent gather, one server's exception is isolated."""
    env = _env()
    server_a = _running_server()
    server_b = _running_server()
    boom = _schedule(
        server_a,
        action=ScheduleAction.STOP,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    ok = _schedule(
        server_b,
        action=ScheduleAction.COMMAND,
        command="say hi",
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server_a)
    env.uow.servers.seed(server_b)
    env.uow.schedules.seed(boom)
    env.uow.schedules.seed(ok)

    base_factory = env.runner.make_execute

    def _make_boom() -> ExecuteScheduleAction:
        base = base_factory()
        return _BoomExecute(
            uow=base.uow,
            send_command=base.send_command,
            start_server=base.start_server,
            stop_server=base.stop_server,
            restart_server=base.restart_server,
            create_backup=base.create_backup,
        )

    env.runner.make_execute = _make_boom

    await env.runner.tick()

    assert _runs(env, ok) == [ScheduleRunOutcome.SUCCESS]
    advanced = env.uow.schedules.by_id[ok.id].next_run_at
    assert advanced is not None and advanced > _NOW


# --- listing-time staleness (issue #1947) -----------------------------------


async def test_listing_time_stale_schedule_still_fires() -> None:
    """A schedule that became due after prev tick is not dropped (#1947).

    Tick at T0, schedule due at T0+1min, next tick at T0+10min: the schedule
    is >LATE_RUN_GRACE late relative to now, but it became due after the
    previous tick started, so it fires.
    """
    env = _env()
    server = _running_server()
    env.uow.servers.seed(server)

    t0 = _NOW
    due_at = t0 + dt.timedelta(minutes=1)  # becomes due 1 minute after T0
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say hi",
        cadence=Cadence.from_interval(_HOUR),
        next_run_at=due_at,
    )
    env.uow.schedules.seed(schedule)

    # First tick at T0: schedule is not yet due.
    env.clock.set(t0)
    await env.runner.tick()
    assert _runs(env, schedule) == []

    # Make it due by setting clock 10 minutes later — well past LATE_RUN_GRACE
    # relative to due_at, but due_at > prev_tick_at (T0), so not stale.
    env.clock.set(t0 + dt.timedelta(minutes=10))
    # The schedule is already stored with next_run_at = due_at which is in the
    # past relative to clock, so list_due will return it.
    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.SUCCESS]


async def test_first_tick_with_stale_schedule_is_skipped() -> None:
    """On the very first tick (prev_tick_at=None), stale schedules are skipped."""
    env = _env()
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say hi",
        next_run_at=_NOW - LATE_RUN_GRACE - dt.timedelta(seconds=1),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    # First tick, no prev_tick_at: the standard staleness applies.
    assert env.control_plane.dispatched == []
    assert _runs(env, schedule) == [ScheduleRunOutcome.SKIPPED]
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.next_run_at is not None and stored.next_run_at > _NOW


# --- backup bounded retry --------------------------------------------------


async def test_failed_backup_retries_once_then_waits_for_next_occurrence() -> None:
    store = FakeBackupArchiveStore(missing=True)  # every backup fails
    env = _env(backup_store=store)
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # scheduled occurrence fails -> arms a retry
    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert len(env.notifier.notifications) == 1
    # Push the next scheduled occurrence out of the way so this test isolates the
    # retry mechanism from the interval grid (the epoch-anchored next occurrence
    # could otherwise land inside the retry window).
    far = _NOW + dt.timedelta(days=1)
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], far
    )
    next_run_after_failure = far

    # ~29 minutes later the retry has not yet come due.
    env.clock.set(_NOW + dt.timedelta(minutes=29))
    await env.runner.tick()
    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]

    # At ~30 minutes the retry fires once, fails, is recorded + notified, and does
    # NOT advance next_run_at (it is a catch-up, not a scheduled occurrence).
    env.clock.set(_NOW + dt.timedelta(minutes=30))
    await env.runner.tick()
    assert _runs(env, schedule) == [
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.FAILURE,
    ]
    # State-transition suppression (#2269): the retry's FAILURE is on the same
    # failing edge as the original occurrence, so it does not re-notify — one toast
    # for the whole incident. The run row is still recorded (above).
    assert len(env.notifier.notifications) == 1
    assert env.uow.schedules.by_id[schedule.id].next_run_at == next_run_after_failure

    # The retry is one-shot: a later tick fires no further retry.
    env.clock.set(_NOW + dt.timedelta(minutes=45))
    await env.runner.tick()
    assert _runs(env, schedule) == [
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.FAILURE,
    ]


async def test_successful_retry_clears_retry_and_is_not_notified() -> None:
    store = FakeBackupArchiveStore(missing=True)
    env = _env(backup_store=store)
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # fails, arms retry
    store._missing = False  # the retry will now succeed
    # Isolate the retry from the interval grid (see the retry test above).
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], _NOW + dt.timedelta(days=1)
    )

    env.clock.set(_NOW + dt.timedelta(minutes=30))
    await env.runner.tick()

    assert _runs(env, schedule) == [
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.SUCCESS,
    ]
    # The original failure notified once; the successful retry is not notified as a
    # failure but, ending the failing run, fires one recovery notification (#2269).
    assert len(env.notifier.notifications) == 2
    assert env.notifier.notifications[0][1] == "schedule_failed"
    assert env.notifier.notifications[1][1] == "schedule_recovered"


async def test_a_retry_whose_claim_raises_stays_armed_for_a_later_tick(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeBackupArchiveStore(missing=True)
    env = _env(backup_store=store)
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    await env.runner.tick()  # fails, arms the retry
    store._missing = False
    # Isolate the retry from the interval grid (see the retry test above).
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], _NOW + dt.timedelta(days=1)
    )

    # The retry's claim cannot read the row once, as on a database still down.
    real_lock = env.uow.schedules.lock
    failures = [RuntimeError("database down")]

    async def failing_lock(schedule_id: ScheduleId) -> Schedule | None:
        if failures:
            raise failures.pop()
        return await real_lock(schedule_id)

    env.uow.schedules.lock = failing_lock  # type: ignore[method-assign]
    retry_at = _NOW + dt.timedelta(minutes=30)
    await _tick_at(env, retry_at)
    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]

    await _tick_at(env, retry_at + _TICK)

    assert _runs(env, schedule) == [
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.SUCCESS,
    ]
    assert len(_lines_naming(caplog, logging.WARNING, schedule)) == 1


async def test_corrupt_backup_failure_is_not_retried() -> None:
    # A BackupCorruptError is provably deterministic (issue #2250): re-archiving
    # the identical corrupt working set can never succeed, so the one-shot retry
    # must NOT be armed. The authoritative signal is the absence of a second run
    # row after the retry delay elapses — a retryable failure would fire the retry
    # and add one. The notification count is only a secondary check here: #2269's
    # state-transition dedup suppresses the sustained-failure toast, so even the
    # buggy (retry-armed) path would emit just one notification; the run-row count
    # is what distinguishes armed from not-armed.
    env = _env(backup_store=_CorruptBackupStore())
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # corrupt-triggered FAILURE; deterministic -> no retry
    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert len(env.notifier.notifications) == 1
    # Push the next scheduled occurrence out of the way so only a retry could add a
    # second run row (isolate from the interval grid, as the retry tests above do).
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], _NOW + dt.timedelta(days=1)
    )

    # Past the retry delay: a retryable failure would fire its one-shot retry here.
    env.clock.set(_NOW + dt.timedelta(minutes=30))
    await env.runner.tick()

    # Primary proof: no second run row — the deterministic failure was never armed
    # for retry.
    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    # Secondary (weaker under #2269 dedup): still exactly one notification.
    assert len(env.notifier.notifications) == 1


# --- a dispatch that timed out: outcome unknown (issue #3660) ---------------


class _TimesOut(FakeControlPlane):
    """Times out each dispatch of the given kinds, as the adapter reports it.

    The control-plane adapter turns a ``CommandTimedOutError`` into a
    ``WorkerUnavailableError`` flagged ``upload_may_be_live``: the command was not
    answered in time on a healthy session, so the Worker may still apply it.
    """

    def __init__(self, *kinds: str, place_to: WorkerId | None = None) -> None:
        super().__init__(place_to=place_to)
        self._timing_out = set(kinds)

    async def _record(
        self, kind: str, worker_id: WorkerId, server_id: ServerId
    ) -> CommandOutcome:
        if kind in self._timing_out:
            self.dispatched.append((kind, worker_id, server_id))
            raise WorkerUnavailableError(str(worker_id.value), upload_may_be_live=True)
        return await super()._record(kind, worker_id, server_id)


@pytest.mark.parametrize(
    ("action", "command"),
    [
        (ScheduleAction.COMMAND, "say hi"),
        (ScheduleAction.STOP, None),
        (ScheduleAction.RESTART, None),
    ],
)
async def test_a_timed_out_dispatch_records_an_unknown_outcome(
    action: ScheduleAction, command: str | None
) -> None:
    # The command, stop or restart was not answered in time, so it may still have
    # run: recorded as unknown, not as a failure.
    env = _env(control_plane=_TimesOut(action.value))
    server = _running_server()
    schedule = _schedule(
        server,
        action=action,
        command=command,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.UNKNOWN]
    assert _run_details(env, schedule) == ["worker timed out"]
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.next_run_at is not None and stored.next_run_at > _NOW


@pytest.mark.parametrize(
    ("action", "command"),
    [
        (ScheduleAction.COMMAND, "say hi"),
        (ScheduleAction.STOP, None),
        (ScheduleAction.RESTART, None),
    ],
)
async def test_a_worker_not_connected_at_dispatch_records_a_failure(
    action: ScheduleAction, command: str | None
) -> None:
    # Nothing was queued: the adapter leaves both flags unset for a Worker with no
    # live session, so the command definitely did not run and the failure stays
    # what it was (issue #3781).
    env = _env(control_plane=FakeControlPlane(unavailable_kinds={action.value}))
    server = _running_server()
    schedule = _schedule(
        server,
        action=action,
        command=command,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert _run_details(env, schedule) == ["worker unavailable"]


async def test_an_unknown_outcome_is_audited_as_an_error_and_not_called_failed() -> (
    None
):
    env = _env(control_plane=_TimesOut("command"))
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say hi",
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert [e.outcome for e in env.audit.events] == [Outcome.ERROR]
    assert len(env.notifier.notifications) == 1
    _server_id, _kind, title, detail = env.notifier.notifications[0]
    assert title == "Scheduled command: outcome unknown"
    assert detail == "worker timed out"


async def test_a_start_whose_start_command_timed_out_is_unknown() -> None:
    # The start was sent, so StartServer keeps the intent and the assignment for
    # the reconciler: the server may well come up.
    env = _env(control_plane=_TimesOut("start", place_to=_WORKER))
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.START,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert env.uow.servers.by_id[server.id].desired_state is DesiredState.RUNNING
    assert _runs(env, schedule) == [ScheduleRunOutcome.UNKNOWN]


async def test_a_scheduled_start_of_a_server_whose_creation_did_not_finish_failed() -> (
    None
):
    # Issue #4065: the refusal is the server's own state, not a state change
    # that raced the runner, so the occurrence is a failure, not a skip.
    env = _env(control_plane=FakeControlPlane(place_to=_WORKER))
    server = _stopped_server()
    server.creation_incomplete = True
    schedule = _schedule(
        server,
        action=ScheduleAction.START,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert env.control_plane.dispatched == []
    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]


async def test_a_start_whose_hydrate_timed_out_failed() -> None:
    # The hydrate runs before the start command, so the start was never sent and
    # StartServer reverted its intent: the start definitely did not happen.
    env = _env(control_plane=_TimesOut("hydrate", place_to=_WORKER))
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.START,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert env.uow.servers.by_id[server.id].desired_state is DesiredState.STOPPED
    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert _run_details(env, schedule) == ["worker timed out"]


async def test_a_backup_whose_snapshot_timed_out_failed_and_retries_once() -> None:
    # CreateBackup archives only after the snapshot returns, so a snapshot that
    # timed out leaves no backup behind, whatever the Worker does with it later.
    # The occurrence failed, and its one-shot retry is the only backup it gets.
    store = FakeBackupArchiveStore()
    env = _env(control_plane=_TimesOut("snapshot"), backup_store=store)
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=True,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert _run_details(env, schedule) == ["worker timed out"]
    assert env.uow.backups.by_id == {}
    assert store.archives == set()
    # Isolate the retry from the interval grid (see the retry tests above).
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], _NOW + dt.timedelta(days=1)
    )

    env.clock.set(_NOW + dt.timedelta(minutes=30))
    await env.runner.tick()

    assert _runs(env, schedule) == [
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.FAILURE,
    ]


async def test_a_backup_whose_worker_disconnected_still_fails_and_retries() -> None:
    # A disconnect is not a timeout: it stays the failure it always was.
    env = _env(control_plane=FakeControlPlane(unavailable_kinds={"snapshot"}))
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=True,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert _run_details(env, schedule) == ["worker unavailable"]
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], _NOW + dt.timedelta(days=1)
    )

    env.clock.set(_NOW + dt.timedelta(minutes=30))
    await env.runner.tick()

    assert _runs(env, schedule) == [
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.FAILURE,
    ]


async def test_an_expired_backup_snapshot_fails_and_retries() -> None:
    # EXPIRED means the Worker definitely did not take the snapshot (issue #3305):
    # a failure, and the one-shot retry still follows.
    env = _env(
        control_plane=FakeControlPlane(
            outcomes={"snapshot": CommandOutcome(status=CommandStatus.EXPIRED)}
        )
    )
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=True,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert _run_details(env, schedule) == ["command_expired"]
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], _NOW + dt.timedelta(days=1)
    )

    env.clock.set(_NOW + dt.timedelta(minutes=30))
    await env.runner.tick()

    assert _runs(env, schedule) == [
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.FAILURE,
    ]


# --- outcome unknown beyond a timeout (issue #3781) --------------------------


class _SessionEnds(FakeControlPlane):
    """Ends the Worker's session while each dispatch of the given kinds awaits
    its answer, as the adapter reports it: a ``WorkerUnavailableError`` flagged
    ``session_ended_in_flight``. The command may have run before the session
    ended."""

    def __init__(self, *kinds: str, place_to: WorkerId | None = None) -> None:
        super().__init__(place_to=place_to)
        self._ending = set(kinds)

    async def _record(
        self, kind: str, worker_id: WorkerId, server_id: ServerId
    ) -> CommandOutcome:
        if kind in self._ending:
            self.dispatched.append((kind, worker_id, server_id))
            raise WorkerUnavailableError(
                str(worker_id.value), session_ended_in_flight=True
            )
        return await super()._record(kind, worker_id, server_id)


@pytest.mark.parametrize(
    ("action", "command"),
    [
        (ScheduleAction.COMMAND, "say hi"),
        (ScheduleAction.STOP, None),
        (ScheduleAction.RESTART, None),
    ],
)
async def test_a_dispatch_whose_session_ended_records_an_unknown_outcome(
    action: ScheduleAction, command: str | None
) -> None:
    # The Worker's session ended while the command awaited its answer: it may
    # have run before the disconnect, so it is unknown, not a failure.
    env = _env(control_plane=_SessionEnds(action.value))
    server = _running_server()
    schedule = _schedule(
        server,
        action=action,
        command=command,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.UNKNOWN]
    assert _run_details(env, schedule) == ["worker disconnected"]


async def test_a_backup_whose_session_ended_at_the_snapshot_still_fails() -> None:
    # The snapshot may have run before the disconnect, but CreateBackup archives
    # only after it returns, so no backup exists: a failure, named for its cause.
    env = _env(control_plane=_SessionEnds("snapshot"))
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=True,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert _run_details(env, schedule) == ["worker disconnected"]
    assert env.uow.backups.by_id == {}


async def test_a_start_the_worker_answered_busy_is_unknown() -> None:
    # BUSY refuses this start command but leaves the intent and the assignment
    # standing, and the reconciler starts the server once the Worker settles.
    env = _env(
        control_plane=FakeControlPlane(
            place_to=_WORKER,
            outcomes={"start": CommandOutcome(status=CommandStatus.BUSY)},
        )
    )
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.START,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert env.uow.servers.by_id[server.id].desired_state is DesiredState.RUNNING
    assert _runs(env, schedule) == [ScheduleRunOutcome.UNKNOWN]
    assert _run_details(env, schedule) == ["worker_busy"]


class _UploadsStayInFlight(FakeStoreGenerationReader):
    """A store whose Worker's uploads are still in flight when the wait runs out."""

    async def uploads_settled(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        worker_id: WorkerId,
        timeout: float,
    ) -> bool:
        return False


async def test_a_start_whose_wait_for_the_workers_uploads_ran_out_is_unknown() -> None:
    # No hydrate was sent, but StartServer keeps the intent and the Worker, and
    # the reconciler retries the start there once the upload has landed (issue
    # #3950). The run names why.
    env = _env(
        control_plane=FakeControlPlane(place_to=_WORKER),
        store_generation=_UploadsStayInFlight(),
    )
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.START,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    row = env.uow.servers.by_id[server.id]
    assert (row.desired_state, row.assigned_worker_id) == (
        DesiredState.RUNNING,
        _WORKER,
    )
    assert env.control_plane.dispatched == []
    assert _runs(env, schedule) == [ScheduleRunOutcome.UNKNOWN]
    assert _run_details(env, schedule) == ["upload_in_flight"]


@pytest.mark.parametrize(
    ("control_plane", "detail"),
    [
        (_SessionEnds("start", place_to=_WORKER), "worker disconnected"),
        (
            FakeControlPlane(place_to=_WORKER, unavailable_kinds={"start"}),
            "worker unavailable",
        ),
    ],
    ids=["session-ended", "not-connected"],
)
async def test_a_start_whose_worker_dropped_at_the_start_command_is_unknown(
    control_plane: FakeControlPlane, detail: str
) -> None:
    # StartServer keeps the intent and the assignment once it reaches the start
    # command, whether or not the command left the API, so the reconciler
    # redispatches it when the Worker is back.
    env = _env(control_plane=control_plane)
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.START,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert env.uow.servers.by_id[server.id].desired_state is DesiredState.RUNNING
    assert _runs(env, schedule) == [ScheduleRunOutcome.UNKNOWN]
    assert _run_details(env, schedule) == [detail]


async def test_a_start_the_worker_refused_failed() -> None:
    # A refusal StartServer compensates reverts the intent: the start definitely
    # did not happen and will not.
    env = _env(
        control_plane=FakeControlPlane(
            place_to=_WORKER,
            outcomes={"start": CommandOutcome(status=CommandStatus.PORT_CONFLICT)},
        )
    )
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.START,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert env.uow.servers.by_id[server.id].desired_state is DesiredState.STOPPED
    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]


class _ReadsFail(FakeServerRepository):
    """A server store whose reads raise once ``failing`` is set, as in an outage."""

    failing = False

    async def get_by_id(self, server_id: ServerId) -> Server | None:
        if self.failing:
            raise ConnectionError("database unavailable")
        return await super().get_by_id(server_id)


class _BusyStartThenReadsFail(FakeControlPlane):
    """Answers the start ``BUSY``, then fails every later server read."""

    def __init__(self, servers: _ReadsFail) -> None:
        super().__init__(
            place_to=_WORKER,
            outcomes={"start": CommandOutcome(status=CommandStatus.BUSY)},
        )
        self._servers = servers

    async def _record(
        self, kind: str, worker_id: WorkerId, server_id: ServerId
    ) -> CommandOutcome:
        outcome = await super()._record(kind, worker_id, server_id)
        if kind == "start":
            self._servers.failing = True
        return outcome


async def test_a_start_whose_outcome_check_raised_fails_and_advances() -> None:
    # The start's row re-read decides unknown from failure. When it raises, the
    # occurrence is still recorded and advanced, or it would re-execute every tick
    # (issue #3799).
    servers = _ReadsFail()
    env = _env(control_plane=_BusyStartThenReadsFail(servers))
    env.uow.servers = servers
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.START,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert _run_details(env, schedule) == ["outcome check failed"]
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.next_run_at is not None and stored.next_run_at > _NOW


async def test_a_stop_the_worker_answered_busy_is_unknown() -> None:
    # BUSY refuses this stop command, but StopServer never reverts its intent:
    # desired=stopped stands, which the reconciler converges (issue #3799).
    env = _env(
        control_plane=FakeControlPlane(
            outcomes={"stop": CommandOutcome(status=CommandStatus.BUSY)}
        )
    )
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.STOP,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert env.uow.servers.by_id[server.id].desired_state is DesiredState.STOPPED
    assert _runs(env, schedule) == [ScheduleRunOutcome.UNKNOWN]
    assert _run_details(env, schedule) == ["worker_busy"]


@pytest.mark.parametrize(
    ("action", "command", "status", "detail"),
    [
        (ScheduleAction.STOP, None, CommandStatus.INTERNAL, "dispatch failed"),
        (ScheduleAction.STOP, None, CommandStatus.EXPIRED, "command_expired"),
        (ScheduleAction.RESTART, None, CommandStatus.BUSY, "worker_busy"),
        (ScheduleAction.COMMAND, "say hi", CommandStatus.BUSY, "worker_busy"),
    ],
    ids=["stop-internal", "stop-expired", "restart-busy", "command-busy"],
)
async def test_a_refusal_other_than_a_busy_stop_still_fails(
    action: ScheduleAction, command: str | None, status: CommandStatus, detail: str
) -> None:
    # Only a busy stop leaves a refused action owed: nothing re-sends a refused
    # restart or command, and a stop the Worker refused otherwise is a settled
    # failure the run history names (issue #3799).
    env = _env(
        control_plane=FakeControlPlane(
            outcomes={action.value: CommandOutcome(status=status)}
        )
    )
    server = _running_server()
    schedule = _schedule(
        server,
        action=action,
        command=command,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert _run_details(env, schedule) == [detail]


def _titles(env: _Env) -> list[str]:
    return [title for _server_id, _kind, title, _detail in env.notifier.notifications]


async def test_a_failure_after_an_unknown_outcome_notifies_once_more() -> None:
    # The incident escalates from "may have run" to "failed", which the operator
    # must hear about. Within each level #2269's suppression still holds: a
    # repeated unknown or failure is silent, and one success recovers, ending the
    # incident: the next failure is a new one and notifies again.
    control_plane = _TimesOut("command")
    env = _env(control_plane=control_plane)
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say hi",
        cadence=Cadence.from_interval(60),
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # unknown
    env.clock.set(_NOW + dt.timedelta(minutes=1))
    await env.runner.tick()  # unknown again
    control_plane._timing_out.clear()
    control_plane._outcomes["command"] = CommandOutcome(status=CommandStatus.INTERNAL)
    env.clock.set(_NOW + dt.timedelta(minutes=2))
    await env.runner.tick()  # failure: an escalation
    env.clock.set(_NOW + dt.timedelta(minutes=3))
    await env.runner.tick()  # failure again
    control_plane._outcomes["command"] = CommandOutcome(status=CommandStatus.OK)
    env.clock.set(_NOW + dt.timedelta(minutes=4))
    await env.runner.tick()  # success
    control_plane._outcomes["command"] = CommandOutcome(status=CommandStatus.INTERNAL)
    env.clock.set(_NOW + dt.timedelta(minutes=5))
    await env.runner.tick()  # failure: a new incident

    assert _runs(env, schedule) == [
        ScheduleRunOutcome.UNKNOWN,
        ScheduleRunOutcome.UNKNOWN,
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.SUCCESS,
        ScheduleRunOutcome.FAILURE,
    ]
    assert _titles(env) == [
        "Scheduled command: outcome unknown",
        "Scheduled command failed",
        "Scheduled command recovered",
        "Scheduled command failed",
    ]


async def test_an_unknown_outcome_after_a_failure_stays_silent() -> None:
    # "May have run" after "failed" is no escalation, and the failure that follows
    # is the same incident's: one toast until the schedule recovers (#2269).
    control_plane = _TimesOut()
    control_plane._outcomes["command"] = CommandOutcome(status=CommandStatus.INTERNAL)
    env = _env(control_plane=control_plane)
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say hi",
        cadence=Cadence.from_interval(60),
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # failure
    control_plane._timing_out.add("command")
    env.clock.set(_NOW + dt.timedelta(minutes=1))
    await env.runner.tick()  # unknown
    control_plane._timing_out.clear()
    env.clock.set(_NOW + dt.timedelta(minutes=2))
    await env.runner.tick()  # failure

    assert _runs(env, schedule) == [
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.UNKNOWN,
        ScheduleRunOutcome.FAILURE,
    ]
    assert _titles(env) == ["Scheduled command failed"]


async def test_a_failing_schedule_stays_silent_across_ticks_it_is_not_due() -> None:
    # The incident lasts until a success, not until the next tick: a failing
    # schedule that is not due on a tick is still failing, and its next failure
    # is the same incident's (#2269).
    refuse = FakeControlPlane(
        outcomes={"command": CommandOutcome(status=CommandStatus.INTERNAL)}
    )
    env = _env(control_plane=refuse)
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say hi",
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # failure
    later = _NOW + dt.timedelta(minutes=30)
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], later
    )
    env.clock.set(_NOW + dt.timedelta(minutes=1))
    await env.runner.tick()  # not due
    env.clock.set(later)
    await env.runner.tick()  # failure again

    assert _runs(env, schedule) == [
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.FAILURE,
    ]
    assert _titles(env) == ["Scheduled command failed"]


async def test_a_schedule_deleted_between_ticks_leaves_no_toast_state() -> None:
    # A schedule deleted while it was failing never reaches the claim or the
    # record that would forget it; the next tick drops its toast state, or the
    # entry would stay until the API restarts (issue #3799).
    refuse = FakeControlPlane(
        outcomes={"command": CommandOutcome(status=CommandStatus.INTERNAL)}
    )
    env = _env(control_plane=refuse)
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say hi",
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    await env.runner.tick()  # failure
    assert schedule.id in env.runner._notifying

    await env.uow.schedules.delete(schedule.id)
    env.clock.set(_NOW + dt.timedelta(minutes=1))
    await env.runner.tick()

    assert schedule.id not in env.runner._notifying


async def test_a_deleted_schedules_pending_retry_still_logs_the_delete(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The toast-state prune leaves a pending backup retry alone: the retry still
    # reaches its claim, which logs the delete once and dispatches nothing
    # (issue #3799, as test_schedule_runner_delete_races pins against Postgres).
    caplog.set_level(logging.INFO)
    env = _env(control_plane=FakeControlPlane(unavailable_kinds={"snapshot"}))
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=True,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    await env.runner.tick()  # failure: arms the retry and the toast state
    dispatched = list(env.control_plane.dispatched)
    await env.uow.schedules.delete(schedule.id)
    caplog.clear()

    env.clock.set(_NOW + dt.timedelta(minutes=30))
    await env.runner.tick()

    assert env.control_plane.dispatched == dispatched
    assert [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
        and str(schedule.id.value) in record.getMessage()
    ] == [
        f"schedule {schedule.id.value} was deleted before its backup retry; "
        "not dispatched"
    ]


class _UnknownExecute(ExecuteScheduleAction):
    """Executor whose every action ends with an unknown outcome."""

    async def __call__(
        self,
        *,
        server_id: ServerId,
        action: ScheduleAction,
        command: str | None,
        only_when_running: bool | None,
    ) -> ActionResult:
        return ActionResult(ScheduleRunOutcome.UNKNOWN, "worker timed out", None)


async def test_an_unknown_backup_outcome_arms_no_retry() -> None:
    # An action that may still have run is never retried: a retry could repeat it.
    env = _env()
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=True,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    base_factory = env.runner.make_execute

    def _make_unknown() -> ExecuteScheduleAction:
        base = base_factory()
        return _UnknownExecute(
            uow=base.uow,
            send_command=base.send_command,
            start_server=base.start_server,
            stop_server=base.stop_server,
            restart_server=base.restart_server,
            create_backup=base.create_backup,
        )

    env.runner.make_execute = _make_unknown

    await env.runner.tick()
    assert _runs(env, schedule) == [ScheduleRunOutcome.UNKNOWN]
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], _NOW + dt.timedelta(days=1)
    )

    env.clock.set(_NOW + dt.timedelta(minutes=30))
    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.UNKNOWN]


# --- history cap + tick isolation -----------------------------------------


async def test_run_history_is_capped() -> None:
    env = _env(history_cap=2)
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say hi",
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    for i in range(1, 4):  # three due occurrences, spaced past the interval
        env.clock.set(_NOW + dt.timedelta(seconds=i * (_HOUR + 100)))
        # Make the schedule due again at the advanced clock.
        stored = env.uow.schedules.by_id[schedule.id]
        env.uow.schedules.by_id[schedule.id] = _replace_next_run(
            stored, env.clock.now() - dt.timedelta(seconds=10)
        )
        await env.runner.tick()

    assert len(_runs(env, schedule)) == 2


class _BoomExecute(ExecuteScheduleAction):
    """Executor that blows up for STOP actions, to test tick isolation."""

    async def __call__(
        self,
        *,
        server_id: ServerId,
        action: ScheduleAction,
        command: str | None,
        only_when_running: bool | None,
    ) -> ActionResult | Deferred:
        if action is ScheduleAction.STOP:
            raise RuntimeError("boom")
        return await super().__call__(
            server_id=server_id,
            action=action,
            command=command,
            only_when_running=only_when_running,
        )


async def test_one_schedule_exception_does_not_stop_the_tick() -> None:
    env = _env()
    # Swap in an executor factory that raises for the stop schedule.
    base_factory = env.runner.make_execute

    def _make_boom() -> ExecuteScheduleAction:
        base = base_factory()
        return _BoomExecute(
            uow=base.uow,
            send_command=base.send_command,
            start_server=base.start_server,
            stop_server=base.stop_server,
            restart_server=base.restart_server,
            create_backup=base.create_backup,
        )

    env.runner.make_execute = _make_boom
    running = _running_server()
    boom = _schedule(
        running,
        action=ScheduleAction.STOP,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    ok = _schedule(
        running,
        action=ScheduleAction.COMMAND,
        command="say hi",
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(running)
    env.uow.schedules.seed(boom)
    env.uow.schedules.seed(ok)

    await env.runner.tick()  # must not raise

    # The healthy schedule still ran and advanced despite the other one crashing.
    assert _runs(env, ok) == [ScheduleRunOutcome.SUCCESS]
    advanced = env.uow.schedules.by_id[ok.id].next_run_at
    assert advanced is not None and advanced > _NOW


# --- player warnings (issue #1839) ----------------------------------------


def _warned_lines(env: _Env) -> list[str]:
    return [line for _server_id, line in env.control_plane.commands]


async def test_due_warning_broadcasts_say_message_without_a_run_row() -> None:
    env = _env()
    server = _running_server()
    occurrence = _NOW + dt.timedelta(minutes=5)
    schedule = _schedule(
        server,
        action=ScheduleAction.STOP,
        next_run_at=occurrence,
        warning_steps=(WarningStep(offset_minutes=5, message="stopping in 5"),),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # now == occurrence - 5min: the 5-minute warn is due

    # Fixed ``say <message>`` broadcast; a warning is best-effort chatter, so no
    # run row, no audit, no notification, and the occurrence itself has not fired.
    assert _warned_lines(env) == ["say stopping in 5"]
    assert _runs(env, schedule) == []
    assert env.audit.events == []
    assert env.notifier.notifications == []
    assert env.uow.schedules.by_id[schedule.id].next_run_at == occurrence


async def test_each_warning_step_broadcasts_once_at_its_offset() -> None:
    env = _env()
    server = _running_server()
    occurrence = _NOW + dt.timedelta(minutes=10)
    schedule = _schedule(
        server,
        action=ScheduleAction.STOP,
        next_run_at=occurrence,
        warning_steps=(
            WarningStep(offset_minutes=10, message="10"),
            WarningStep(offset_minutes=5, message="5"),
            WarningStep(offset_minutes=1, message="1"),
        ),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # T-10
    env.clock.set(occurrence - dt.timedelta(minutes=5))
    await env.runner.tick()  # T-5
    env.clock.set(occurrence - dt.timedelta(minutes=1))
    await env.runner.tick()  # T-1

    assert _warned_lines(env) == ["say 10", "say 5", "say 1"]
    assert _runs(env, schedule) == []


async def test_warning_not_rebroadcast_on_a_later_tick() -> None:
    env = _env()
    server = _running_server()
    occurrence = _NOW + dt.timedelta(minutes=5)
    schedule = _schedule(
        server,
        action=ScheduleAction.STOP,
        next_run_at=occurrence,
        warning_steps=(WarningStep(offset_minutes=5, message="soon"),),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # T-5: broadcast once
    env.clock.set(occurrence - dt.timedelta(minutes=4))
    await env.runner.tick()  # still within grace, but already sent

    assert _warned_lines(env) == ["say soon"]


async def test_a_stored_command_past_the_console_limit_fails_its_run() -> None:
    # A command written before the console-line bound existed is not migrated; its
    # occurrence fails with a reason that names the cause, and nothing is sent
    # (issue #3363).
    env = _env()
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say " + "a" * MAX_CONSOLE_LINE_BYTES,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert env.control_plane.dispatched == []
    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert _run_details(env, schedule) == ["console_line_too_long"]


async def test_a_stored_warning_past_the_console_limit_is_skipped() -> None:
    # The same for a warning step's message: its broadcast is skipped, the step
    # beside it and the occurrence it heralds are unaffected (issue #3363).
    env = _env()
    server = _running_server()
    occurrence = _NOW + dt.timedelta(minutes=5)
    schedule = _schedule(
        server,
        action=ScheduleAction.STOP,
        next_run_at=occurrence,
        warning_steps=(
            WarningStep(offset_minutes=5, message="a" * MAX_CONSOLE_LINE_BYTES),
            WarningStep(offset_minutes=1, message="1"),
        ),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # T-5: over the limit, skipped
    env.clock.set(occurrence - dt.timedelta(minutes=1))
    await env.runner.tick()  # T-1
    env.clock.set(occurrence)
    await env.runner.tick()  # T: the stop occurrence fires

    assert _warned_lines(env) == ["say 1"]
    assert _runs(env, schedule) == [ScheduleRunOutcome.SUCCESS]


async def test_warnings_broadcast_then_the_occurrence_stops_the_server() -> None:
    env = _env()
    server = _running_server()
    occurrence = _NOW + dt.timedelta(minutes=5)
    schedule = _schedule(
        server,
        action=ScheduleAction.STOP,
        next_run_at=occurrence,
        warning_steps=(
            WarningStep(offset_minutes=5, message="5"),
            WarningStep(offset_minutes=1, message="1"),
        ),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # T-5
    env.clock.set(occurrence - dt.timedelta(minutes=1))
    await env.runner.tick()  # T-1
    env.clock.set(occurrence)
    await env.runner.tick()  # T: the stop occurrence fires

    assert _warned_lines(env) == ["say 5", "say 1"]
    # The occurrence executed exactly once as a stop and recorded success.
    assert [k for k, *_ in env.control_plane.dispatched].count("stop") == 1
    assert _runs(env, schedule) == [ScheduleRunOutcome.SUCCESS]


async def test_warning_skipped_when_server_offline_does_not_affect_the_run() -> None:
    env = _env()
    server = _stopped_server()
    occurrence = _NOW + dt.timedelta(minutes=5)
    schedule = _schedule(
        server,
        action=ScheduleAction.STOP,
        next_run_at=occurrence,
        warning_steps=(WarningStep(offset_minutes=5, message="soon"),),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # server offline at warn time

    # Nothing broadcast, and no run row / audit / notification; the occurrence
    # is untouched (its next_run_at unchanged).
    assert _warned_lines(env) == []
    assert env.control_plane.dispatched == []
    assert _runs(env, schedule) == []
    assert env.notifier.notifications == []
    assert env.uow.schedules.by_id[schedule.id].next_run_at == occurrence


async def test_warning_dispatch_failure_is_swallowed() -> None:
    refuse = FakeControlPlane(
        outcomes={"command": CommandOutcome(status=CommandStatus.INTERNAL)}
    )
    env = _env(control_plane=refuse)
    server = _running_server()
    occurrence = _NOW + dt.timedelta(minutes=5)
    schedule = _schedule(
        server,
        action=ScheduleAction.STOP,
        next_run_at=occurrence,
        warning_steps=(WarningStep(offset_minutes=5, message="soon"),),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # the warn dispatch is refused by the worker

    # A send failure is logged and skipped: no run row, no notification, and the
    # occurrence is untouched. The failed step is consumed (not retried forever).
    assert _runs(env, schedule) == []
    assert env.notifier.notifications == []
    assert env.uow.schedules.by_id[schedule.id].next_run_at == occurrence


async def test_schedule_enabled_inside_window_sends_only_steps_still_ahead() -> None:
    env = _env()
    server = _running_server()
    # First seen ~3 minutes before T: the 5-minute warn instant is already two
    # minutes past (dropped), the 1-minute warn is still two minutes ahead (fires).
    occurrence = _NOW + dt.timedelta(minutes=3)
    schedule = _schedule(
        server,
        action=ScheduleAction.STOP,
        next_run_at=occurrence,
        warning_steps=(
            WarningStep(offset_minutes=5, message="5"),
            WarningStep(offset_minutes=1, message="1"),
        ),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # T-3: the 5-minute warn is stale, the 1-minute not yet due
    assert _warned_lines(env) == []

    env.clock.set(occurrence - dt.timedelta(minutes=1))
    await env.runner.tick()  # T-1: only the 1-minute warning
    assert _warned_lines(env) == ["say 1"]


async def test_no_warning_for_a_past_due_occurrence() -> None:
    env = _env()
    server = _running_server()
    # The occurrence is already due — handled by the due poll, not the look-ahead —
    # so its warnings are in the past and must not fire late.
    schedule = _schedule(
        server,
        action=ScheduleAction.STOP,
        next_run_at=_NOW - dt.timedelta(seconds=10),
        warning_steps=(WarningStep(offset_minutes=5, message="5"),),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _warned_lines(env) == []


def _missed_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "missed its send window" in r.message]


async def test_warning_window_between_coarse_ticks_is_logged_not_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The review repro: a 120 s tick (effective grace 120 s) and a 1-minute
    # warning whose whole send window [T-60, T) falls between two ticks. The
    # step cannot broadcast, but it must be observable — logged once at warning
    # level — and the stop at T must be unaffected.
    env = _env(warning_grace=dt.timedelta(seconds=120))
    server = _running_server()
    occurrence = _NOW + dt.timedelta(seconds=120)
    schedule = _schedule(
        server,
        action=ScheduleAction.STOP,
        next_run_at=occurrence,
        warning_steps=(WarningStep(offset_minutes=1, message="soon"),),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # T-120: the warn instant (T-60) has not arrived
    env.clock.set(occurrence)
    await env.runner.tick()  # T: the occurrence fires; the window was skipped

    assert _warned_lines(env) == []
    assert _runs(env, schedule) == [ScheduleRunOutcome.SUCCESS]
    missed = _missed_logs(caplog)
    assert len(missed) == 1
    assert missed[0].levelno == logging.WARNING
    assert str(schedule.id.value) in missed[0].getMessage()
    assert "1-minute" in missed[0].getMessage()


async def test_warning_offset_above_coarse_tick_fires_late_but_before_t() -> None:
    # With the effective grace matching a coarse tick, a warn instant that fell
    # between ticks still broadcasts on the next tick — late, but strictly
    # before the occurrence.
    env = _env(warning_grace=dt.timedelta(seconds=120))
    server = _running_server()
    occurrence = _NOW + dt.timedelta(seconds=200)
    # 5-minute warning: its warn instant (T-300) is 100 s past at this tick —
    # beyond the 60 s floor, within the derived 120 s grace.
    schedule = _schedule(
        server,
        action=ScheduleAction.STOP,
        next_run_at=occurrence,
        warning_steps=(WarningStep(offset_minutes=5, message="soon"),),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _warned_lines(env) == ["say soon"]
    # Only the warning broadcast — the stop itself has not fired yet.
    assert [k for k, *_ in env.control_plane.dispatched] == ["command"]
    assert _runs(env, schedule) == []


async def test_stale_step_of_late_enabled_schedule_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = _env()
    server = _running_server()
    # First seen 3 minutes before T: the 5-minute step's window ended at T-4.
    occurrence = _NOW + dt.timedelta(minutes=3)
    schedule = _schedule(
        server,
        action=ScheduleAction.STOP,
        next_run_at=occurrence,
        warning_steps=(
            WarningStep(offset_minutes=5, message="5"),
            WarningStep(offset_minutes=1, message="1"),
        ),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # T-3: the 5-minute step is already unsendable
    env.clock.set(occurrence - dt.timedelta(minutes=2))
    await env.runner.tick()  # T-2: consumed — must not log again

    assert _warned_lines(env) == []
    missed = _missed_logs(caplog)
    assert len(missed) == 1
    assert "5-minute" in missed[0].getMessage()

    env.clock.set(occurrence - dt.timedelta(minutes=1))
    await env.runner.tick()  # T-1: the still-ahead step fires normally

    assert _warned_lines(env) == ["say 1"]
    assert len(_missed_logs(caplog)) == 1


async def test_sent_state_prunes_entries_for_past_occurrences() -> None:
    env = _env()
    schedule_id = ScheduleId.new()
    past = (schedule_id, _NOW - dt.timedelta(minutes=1), 5)
    future = (schedule_id, _NOW + dt.timedelta(minutes=10), 5)
    env.runner._warned = {past, future}

    await env.runner.tick()

    # Entries for past occurrences are dropped each tick so the in-memory
    # sent-state stays bounded; still-future ones survive.
    assert env.runner._warned == {future}


def _replace_next_run(schedule: Schedule, next_run_at: dt.datetime) -> Schedule:
    return replace(schedule, next_run_at=next_run_at)


# --- retention prune after a successful backup run (issue #1841) ------------


def _scheduled_backup(env: _Env, server: Server, created_at: dt.datetime) -> Backup:
    backup = Backup(
        id=BackupId.new(),
        server_id=server.id,
        storage_ref=f"ref-{uuid.uuid4().hex}",
        size_bytes=1,
        source=BackupSource.SCHEDULED,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=created_at,
    )
    env.uow.backups.seed(backup)
    return backup


def _scheduled_rows(env: _Env, server: Server) -> list[Backup]:
    return [
        b
        for b in env.uow.backups.by_id.values()
        if b.server_id == server.id and b.source is BackupSource.SCHEDULED
    ]


async def test_successful_backup_run_prunes_per_retention_policy() -> None:
    # With keep-1 configured, the backup the due occurrence just created is
    # kept and the pre-existing scheduled backup is pruned — while a manual
    # backup survives untouched.
    env = _env()
    server = _stopped_server()
    server.backup_retention = {"keep_last": 1}
    env.uow.servers.seed(server)
    old = _scheduled_backup(env, server, _NOW - dt.timedelta(days=1))
    manual = Backup(
        id=BackupId.new(),
        server_id=server.id,
        storage_ref="manual-ref",
        size_bytes=1,
        source=BackupSource.MANUAL,
        health=BackupHealth.HEALTHY,
        created_by=uuid.uuid4(),
        created_at=_NOW - dt.timedelta(days=30),
    )
    env.uow.backups.seed(manual)
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.SUCCESS]
    remaining = _scheduled_rows(env, server)
    assert len(remaining) == 1
    assert remaining[0].id != old.id  # the fresh backup survived, the old went
    assert manual.id in env.uow.backups.by_id
    # No notification for the prune (owner spec: run failures only).
    assert env.notifier.notifications == []


async def test_prune_failure_does_not_fail_the_successful_backup_run() -> None:
    class _FailingDeleteStore(FakeBackupArchiveStore):
        async def delete(
            self,
            *,
            community_id: CommunityId,
            server_id: ServerId,
            storage_ref: str,
        ) -> None:
            raise RuntimeError("storage down")

    env = _env(backup_store=_FailingDeleteStore())
    server = _stopped_server()
    server.backup_retention = {"keep_last": 1}
    env.uow.servers.seed(server)
    _scheduled_backup(env, server, _NOW - dt.timedelta(days=1))
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    # The run stays a recorded SUCCESS and nothing is notified: the prune
    # failure is isolated (owner spec).
    assert _runs(env, schedule) == [ScheduleRunOutcome.SUCCESS]
    assert env.notifier.notifications == []
    # Nothing was pruned (the archive delete failed before the row delete).
    assert len(_scheduled_rows(env, server)) == 2


async def test_skipped_backup_run_does_not_prune() -> None:
    env = _env()
    server = _server(
        desired=DesiredState.RUNNING, observed=ObservedState.STARTING, worker=_WORKER
    )
    server.backup_retention = {"keep_last": 1}
    env.uow.servers.seed(server)
    old = _scheduled_backup(env, server, _NOW - dt.timedelta(days=1))
    extra = _scheduled_backup(env, server, _NOW - dt.timedelta(days=2))
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.SKIPPED]
    assert {old.id, extra.id} <= set(env.uow.backups.by_id)


async def test_failed_backup_run_does_not_prune() -> None:
    # Queue semantics (#2258, reverting #2254): a scheduled backup rolls off
    # ONLY when a new backup succeeds. A failed BACKUP run must never invoke
    # retention — deleting recovery points while backups are failing is exactly
    # the data-loss the owner directive forbids.
    env = _env(backup_store=_DiskFullBackupStore())
    server = _stopped_server()
    server.backup_retention = {"keep_last": 1}
    env.uow.servers.seed(server)
    old = _scheduled_backup(env, server, _NOW - dt.timedelta(days=1))
    extra = _scheduled_backup(env, server, _NOW - dt.timedelta(days=2))
    spy = _SpyPrune(
        uow=env.uow,
        backup_store=FakeBackupArchiveStore(),
        audit=env.audit,
        clock=env.clock,
    )
    env.runner.prune_backups = spy
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    # The run failed (disk full) and prune was never called: no backup deleted.
    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert spy.calls == []
    assert {old.id, extra.id} <= set(env.uow.backups.by_id)


async def test_successful_backup_retry_also_prunes() -> None:
    store = FakeBackupArchiveStore(missing=True)
    env = _env(backup_store=store)
    server = _stopped_server()
    server.backup_retention = {"keep_last": 1}
    env.uow.servers.seed(server)
    old = _scheduled_backup(env, server, _NOW - dt.timedelta(days=1))
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        only_when_running=False,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.schedules.seed(schedule)

    await env.runner.tick()  # fails, arms the one-shot retry; nothing pruned
    assert old.id in env.uow.backups.by_id
    store._missing = False  # the retry will now succeed
    # Isolate the retry from the interval grid (see the retry tests above).
    env.uow.schedules.by_id[schedule.id] = _replace_next_run(
        env.uow.schedules.by_id[schedule.id], _NOW + dt.timedelta(days=1)
    )

    env.clock.set(_NOW + dt.timedelta(minutes=30))
    await env.runner.tick()

    assert _runs(env, schedule) == [
        ScheduleRunOutcome.FAILURE,
        ScheduleRunOutcome.SUCCESS,
    ]
    # The retry's successful backup triggered the prune of the older one.
    remaining = _scheduled_rows(env, server)
    assert len(remaining) == 1
    assert remaining[0].id != old.id


# --- hydration isolation (issue #1856) ------------------------------------


def test_safe_hydrate_skips_unloadable_rows_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ScheduleModel with invalid cadence data is skipped, not propagated.

    Defense-in-depth (issue #1856): a corrupted row (e.g. interval_seconds
    below the domain floor from a past migration bug or a manual DB edit) must
    not prevent the remaining valid rows from loading.
    """

    from mc_server_dashboard_api.servers.adapters.schedule_models import ScheduleModel
    from mc_server_dashboard_api.servers.adapters.schedule_repository import (
        _safe_hydrate,
    )

    good_id = uuid.uuid4()
    bad_id = uuid.uuid4()
    server_id = uuid.uuid4()

    good_row = ScheduleModel(
        id=good_id,
        server_id=server_id,
        name="nightly-backup",
        action="backup",
        payload={},
        cron=None,
        interval_seconds=3600,
        timezone="UTC",
        enabled=True,
        next_run_at=_NOW,
        last_run_at=None,
        created_by=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    bad_row = ScheduleModel(
        id=bad_id,
        server_id=server_id,
        name="corrupted",
        action="backup",
        payload={},
        cron=None,
        interval_seconds=0,  # below the MIN_INTERVAL_SECONDS floor
        timezone="UTC",
        enabled=True,
        next_run_at=_NOW,
        last_run_at=None,
        created_by=None,
        created_at=_NOW,
        updated_at=_NOW,
    )

    with caplog.at_level(logging.WARNING):
        result, failed_ids = _safe_hydrate([bad_row, good_row])

    # The valid row survived; the bad one was dropped and reported for quarantine.
    assert len(result) == 1
    assert result[0].id.value == good_id
    assert failed_ids == [bad_id]

    # A warning was logged for the bad row, identifying it by id.
    hydration_warnings = [r for r in caplog.records if "failed to hydrate" in r.message]
    assert len(hydration_warnings) == 1
    assert str(bad_id) in hydration_warnings[0].getMessage()


async def test_tick_commits_its_poll_transactions() -> None:
    """The due poll and the warning poll each commit their transaction (#2150).

    A corrupt row is quarantined (``enabled = false``) by a staged UPDATE inside
    ``list_due`` / ``list_warning_candidates``; the runner must commit the poll's
    unit of work or the quarantine would roll back and respool every tick. With
    no schedules seeded both polls are empty, so the only commits are the two
    poll-transaction commits.
    """

    env = _env()
    await env.runner.tick()
    assert env.uow.commits == 2


# --- advance CAS guard (issue #1963) ----------------------------------------


async def test_advance_does_not_clobber_a_concurrently_edited_cadence() -> None:
    """A concurrent PATCH that recomputed next_run_at wins (#1963)."""
    # A schedule that was due when the runner's list_due read it.
    original_next = _NOW - dt.timedelta(seconds=10)
    patched_next = _NOW + dt.timedelta(hours=5)

    # Use a FakeControlPlane subclass that mutates the schedule store mid-dispatch,
    # simulating a concurrent PATCH that recomputes next_run_at.
    class _RacingControlPlane(FakeControlPlane):
        def __init__(self, uow: FakeUnitOfWork, schedule_id: ScheduleId) -> None:
            super().__init__()
            self._uow = uow
            self._schedule_id = schedule_id

        async def command(
            self, *, worker_id: WorkerId, server_id: ServerId, line: str
        ) -> CommandOutcome:
            # Simulate the concurrent PATCH arriving between list_due and _advance.
            stored = self._uow.schedules.by_id[self._schedule_id]
            self._uow.schedules.by_id[self._schedule_id] = replace(
                stored, next_run_at=patched_next
            )
            return await super().command(
                worker_id=worker_id, server_id=server_id, line=line
            )

    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say hi",
        cadence=Cadence.from_interval(_HOUR),
        next_run_at=original_next,
    )

    cp = _RacingControlPlane(FakeUnitOfWork(), schedule.id)
    env = _env(control_plane=cp)
    # Re-attach the control plane's uow reference after _env builds the real one.
    cp._uow = env.uow
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    # The command still executed (that happened before the advance).
    assert [k for k, *_ in env.control_plane.dispatched] == ["command"]
    # But the advance was a no-op because the CAS on fired_occurrence failed:
    # next_run_at is still the patched value, not a stale hourly advance.
    final = env.uow.schedules.by_id[schedule.id]
    assert final.next_run_at == patched_next


# --- claim before dispatch (issue #3314) ------------------------------------


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param({"enabled": False, "next_run_at": None}, id="disabled"),
        pytest.param({"next_run_at": _NOW + dt.timedelta(hours=5)}, id="rescheduled"),
    ],
)
async def test_schedule_edited_after_the_poll_is_not_dispatched(
    edit: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    env = _env()
    server = _running_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.COMMAND,
        command="say hi",
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)
    poll = env.uow.schedules.list_due

    async def list_then_edit(now: dt.datetime) -> list[Schedule]:
        due = await poll(now)
        # A PATCH committing between the poll and the claim.
        stored = env.uow.schedules.by_id[schedule.id]
        env.uow.schedules.by_id[schedule.id] = replace(stored, **edit)  # type: ignore[arg-type]
        return due

    env.uow.schedules.list_due = list_then_edit  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO):
        await env.runner.tick()

    # The occurrence the poll read no longer exists: nothing ran or was recorded.
    assert env.control_plane.dispatched == []
    assert _runs(env, schedule) == []
    assert [r.levelno for r in caplog.records if "not dispatched" in r.message] == [
        logging.INFO
    ]
