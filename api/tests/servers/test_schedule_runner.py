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
import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field, replace

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
    warning_steps: tuple[WarningStep, ...] = (),
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
    warning_grace: dt.timedelta = WARNING_GRACE_FLOOR,
) -> _Env:
    uow = FakeUnitOfWork()
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
        make_execute=make_execute,
        send_command=SendServerCommand(uow=uow, control_plane=cp),
        calculator=CronsimNextRunCalculator(),
        audit=audit,
        notifier=notifier,
        clock=the_clock,
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


async def test_backup_storage_timeout_records_distinguishable_detail() -> None:
    env = _env(backup_store=_StorageTimeoutBackupStore())
    server = _stopped_server()
    schedule = _schedule(
        server,
        action=ScheduleAction.BACKUP,
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
        next_run_at=_NOW - dt.timedelta(seconds=10),
    )
    env.uow.servers.seed(server)
    env.uow.schedules.seed(schedule)

    await env.runner.tick()

    assert _runs(env, schedule) == [ScheduleRunOutcome.FAILURE]
    assert _run_details(env, schedule) == ["backup corrupt"]


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
            ) -> ActionResult:
                nonlocal max_concurrent
                in_flight.add(server_id)
                max_concurrent = max(max_concurrent, len(in_flight))
                await asyncio.sleep(0)  # yield to allow true concurrency
                result = await ExecuteScheduleAction.__call__(
                    self, server_id=server_id, action=action, command=command
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
            ) -> ActionResult:
                # Use command line as schedule identifier since we can't get
                # schedule id here.
                key = ScheduleId(uuid.uuid4())  # unique per call
                in_flight.add(key)
                in_flight_count.append(len(in_flight))
                await asyncio.sleep(0)
                result = await ExecuteScheduleAction.__call__(
                    self, server_id=server_id, action=action, command=command
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
            ) -> ActionResult:
                nonlocal max_concurrent, in_flight
                in_flight += 1
                max_concurrent = max(max_concurrent, in_flight)
                await asyncio.sleep(0)  # yield to let all allowed tasks enter
                result = await ExecuteScheduleAction.__call__(
                    self, server_id=server_id, action=action, command=command
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
        result = _safe_hydrate([bad_row, good_row])

    # The valid row survived; the bad one was silently dropped.
    assert len(result) == 1
    assert result[0].id.value == good_id

    # A warning was logged for the bad row, identifying it by id.
    hydration_warnings = [r for r in caplog.records if "failed to hydrate" in r.message]
    assert len(hydration_warnings) == 1
    assert str(bad_id) in hydration_warnings[0].getMessage()


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
