"""Use-case tests for the general-scheduler runner (epic #649, issue #1838).

Drives :class:`RunScheduleTick` (wrapping the real :class:`ExecuteScheduleAction`
over the existing lifecycle/command/backup use cases) against in-memory fakes and
a faked clock, covering the outcome taxonomy (success / skipped / failure), the
missed-run semantics (overdue backup coalesces to one catch-up; overdue non-backup
does not fire late), the bounded backup retry, the run-history cap, audit +
notification emission, and per-schedule tick isolation.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, replace

from mc_server_dashboard_api.audit.domain.events import Outcome
from mc_server_dashboard_api.audit.domain.operations import (
    SCHEDULE_RUN,
    TARGET_SCHEDULE,
)
from mc_server_dashboard_api.servers.adapters.cronsim_next_run_calculator import (
    CronsimNextRunCalculator,
)
from mc_server_dashboard_api.servers.application.backups import CreateBackup
from mc_server_dashboard_api.servers.application.lifecycle import (
    RestartServer,
    SendServerCommand,
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.application.schedule_runner import (
    LATE_RUN_GRACE,
    ActionResult,
    ExecuteScheduleAction,
    RunScheduleTick,
)
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    SnapshotServer,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.schedule import (
    Cadence,
    Schedule,
    ScheduleAction,
    ScheduleId,
    ScheduleRunOutcome,
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
    FakeBackupArchiveStore,
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeServerNotifier,
    FakeStoreGenerationReader,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 7, 11, 12, 0, 30, tzinfo=dt.timezone.utc)
_HOUR = 3600
_WORKER = WorkerId(uuid.uuid4())


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
    history_cap: int = 50,
    backup_retry_delay: dt.timedelta = dt.timedelta(minutes=30),
) -> _Env:
    uow = FakeUnitOfWork()
    cp = control_plane or FakeControlPlane()
    the_clock = clock or FakeClock(_NOW)
    store = backup_store or FakeBackupArchiveStore()
    execute = ExecuteScheduleAction(
        uow=uow,
        send_command=SendServerCommand(uow=uow, control_plane=cp),
        start_server=StartServer(
            uow=uow,
            control_plane=cp,
            clock=the_clock,
            jar_provisioner=FakeJarProvisioner(),
            store_generation=FakeStoreGenerationReader(),
            file_store=FakeFileStore(),
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
        execute=execute,
        calculator=CronsimNextRunCalculator(),
        audit=audit,
        notifier=notifier,
        clock=the_clock,
        history_cap=history_cap,
        backup_retry_delay=backup_retry_delay,
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


async def test_backup_on_transitional_server_is_skipped() -> None:
    env = _env()
    server = _server(
        desired=DesiredState.RUNNING, observed=ObservedState.STARTING, worker=_WORKER
    )
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.SKIPPED]
    assert env.notifier.notifications == []
    assert await env.uow.backups.list_for_server(server.id) == []


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


class _DiskFullBackupStore(FakeBackupArchiveStore):
    """Raises a non-ServerError mid-archive (the disk-full case)."""

    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        raise OSError("disk full")


async def test_unexpected_exception_is_classified_as_failure() -> None:
    env = _env(backup_store=_DiskFullBackupStore())
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
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
    base = env.runner.execute

    class _DisableDuringRun(ExecuteScheduleAction):
        """Simulates a CRUD disable landing while the action executes."""

        async def __call__(
            self, *, server_id: ServerId, action: ScheduleAction, command: str | None
        ) -> ActionResult:
            env.uow.schedules.by_id[schedule.id] = replace(
                env.uow.schedules.by_id[schedule.id],
                enabled=False,
                next_run_at=None,
            )
            return await ExecuteScheduleAction.__call__(
                self, server_id=server_id, action=action, command=command
            )

    env.runner.execute = _DisableDuringRun(
        uow=base.uow,
        send_command=base.send_command,
        start_server=base.start_server,
        stop_server=base.stop_server,
        restart_server=base.restart_server,
        create_backup=base.create_backup,
    )

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


async def test_overdue_lifecycle_schedule_does_not_fire_late() -> None:
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

    # No execution, no run row; next_run advanced past now; last_run untouched.
    assert env.control_plane.dispatched == []
    assert _runs(env, schedule) == []
    stored = env.uow.schedules.by_id[schedule.id]
    assert stored.last_run_at is None
    assert stored.next_run_at is not None and stored.next_run_at > _NOW


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
    assert _runs(env, schedule) == []
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
    assert len(env.notifier.notifications) == 2
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
    # Only the original failure was notified; the successful retry was not.
    assert len(env.notifier.notifications) == 1


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
        self, *, server_id: ServerId, action: ScheduleAction, command: str | None
    ) -> ActionResult:
        if action is ScheduleAction.STOP:
            raise RuntimeError("boom")
        return await super().__call__(
            server_id=server_id, action=action, command=command
        )


async def test_one_schedule_exception_does_not_stop_the_tick() -> None:
    env = _env()
    # Swap in an executor that raises for the stop schedule.
    env.runner.execute = _BoomExecute(
        uow=env.runner.execute.uow,
        send_command=env.runner.execute.send_command,
        start_server=env.runner.execute.start_server,
        stop_server=env.runner.execute.stop_server,
        restart_server=env.runner.execute.restart_server,
        create_backup=env.runner.execute.create_backup,
    )
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


def _replace_next_run(schedule: Schedule, next_run_at: dt.datetime) -> Schedule:
    return replace(schedule, next_run_at=next_run_at)
