"""Use-case tests for the periodic scheduled-backup scheduler (FR-BAK-3).

Drives :class:`RunBackupScheduleTick` against in-memory fakes with a faked clock:
a scheduled server is not backed up on the first tick (herd guard), is backed up
once due, an unscheduled server is ignored, a failed backup is retried on the next
tick, and the candidate set spans both at-rest and running servers (unlike the
snapshot scheduler).
"""

from __future__ import annotations

import datetime as dt
import uuid

from mc_server_dashboard_api.servers.application.backup_scheduler import (
    RunBackupScheduleTick,
)
from mc_server_dashboard_api.servers.application.backups import CreateBackup
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    SnapshotServer,
)
from mc_server_dashboard_api.servers.domain.backup import BackupSource
from mc_server_dashboard_api.servers.domain.backup_schedule import (
    BACKUP_INTERVAL_CONFIG_KEY,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from tests.servers.fakes import (
    FakeBackupArchiveStore,
    FakeClock,
    FakeControlPlane,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_HOUR = 3600


def _server(
    *,
    desired: DesiredState,
    observed: ObservedState,
    worker: WorkerId | None,
    config: dict[str, object] | None = None,
) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config=config or {},
        desired_state=desired,
        observed_state=observed,
        observed_at=None,
        assigned_worker_id=worker,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _scheduled_at_rest(hours: int = 1) -> Server:
    return _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.STOPPED,
        worker=None,
        config={BACKUP_INTERVAL_CONFIG_KEY: hours},
    )


def _build(
    uow: FakeUnitOfWork, archive: FakeBackupArchiveStore, clock: FakeClock
) -> RunBackupScheduleTick:
    control_plane = FakeControlPlane()
    create = CreateBackup(
        uow=uow,
        backup_store=archive,
        snapshot_server=SnapshotServer(uow=uow, control_plane=control_plane),
        clock=clock,
    )
    return RunBackupScheduleTick(uow=uow, create_backup=create, clock=clock)


async def test_first_tick_does_not_back_up_immediately() -> None:
    uow = FakeUnitOfWork()
    uow.servers.seed(_scheduled_at_rest())
    archive = FakeBackupArchiveStore()
    scheduler = _build(uow, archive, FakeClock(_NOW))
    await scheduler.tick()
    assert archive.created == []


async def test_due_scheduled_server_is_backed_up() -> None:
    uow = FakeUnitOfWork()
    server = _scheduled_at_rest(hours=1)
    uow.servers.seed(server)
    archive = FakeBackupArchiveStore()
    clock = FakeClock(_NOW)
    scheduler = _build(uow, archive, clock)
    await scheduler.tick()  # schedules first due instant
    clock.set(_NOW + dt.timedelta(seconds=_HOUR))  # past interval + jitter
    await scheduler.tick()
    assert archive.created == [server.id]
    # The execution is recorded as a scheduled backup row (the history).
    rows = await uow.backups.list_for_server(server.id)
    assert len(rows) == 1
    assert rows[0].source is BackupSource.SCHEDULED
    assert rows[0].created_by is None


async def test_unscheduled_server_is_ignored() -> None:
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            desired=DesiredState.STOPPED,
            observed=ObservedState.STOPPED,
            worker=None,
        )
    )
    archive = FakeBackupArchiveStore()
    clock = FakeClock(_NOW)
    scheduler = _build(uow, archive, clock)
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(seconds=_HOUR))
    await scheduler.tick()
    assert archive.created == []


async def test_failed_backup_is_retried_next_tick() -> None:
    uow = FakeUnitOfWork()
    server = _scheduled_at_rest(hours=1)
    uow.servers.seed(server)
    # Nothing published -> CreateBackup raises BackupNotFoundError, caught + retried.
    archive = FakeBackupArchiveStore(missing=True)
    clock = FakeClock(_NOW)
    scheduler = _build(uow, archive, clock)
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(seconds=_HOUR))
    await scheduler.tick()  # fails (next-due stays in the past)
    assert await uow.backups.list_for_server(server.id) == []
    # The failure does not advance next-due, so the next tick tries again.
    archive._missing = False
    clock.set(_NOW + dt.timedelta(seconds=_HOUR + 1))
    await scheduler.tick()
    assert archive.created == [server.id]


async def test_running_scheduled_server_is_backed_up_via_snapshot() -> None:
    uow = FakeUnitOfWork()
    worker = WorkerId(uuid.uuid4())
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
        worker=worker,
        config={BACKUP_INTERVAL_CONFIG_KEY: 1},
    )
    uow.servers.seed(server)
    control_plane = FakeControlPlane()
    create = CreateBackup(
        uow=uow,
        backup_store=FakeBackupArchiveStore(),
        snapshot_server=SnapshotServer(uow=uow, control_plane=control_plane),
        clock=FakeClock(_NOW),
    )
    clock = FakeClock(_NOW)
    scheduler = RunBackupScheduleTick(uow=uow, create_backup=create, clock=clock)
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(seconds=_HOUR))
    await scheduler.tick()
    # The running path dispatches a snapshot (worker quiesces safely).
    assert [k for k, *_ in control_plane.dispatched] == ["snapshot"]
    rows = await uow.backups.list_for_server(server.id)
    assert len(rows) == 1
