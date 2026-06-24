"""Use-case tests for the one-shot fsck/quarantine sweep (issue #744).

Exercises :class:`IntegritySweep` against fakes (no DB, no real Storage), per
TESTING.md Section 4. The sweep enumerates every server, re-checks each backup
(extract-and-fsck) and persists ``HEALTHY`` / ``QUARANTINED`` on the backup row,
fscks the published ``current`` snapshot (report/audit only — snapshots are
filesystem-only), audits each quarantine, and returns a summary. Re-running yields
the same classification (idempotent).
"""

from __future__ import annotations

import datetime as dt
import uuid

from mc_server_dashboard_api.audit.domain.operations import (
    BACKUP_QUARANTINE,
    SNAPSHOT_QUARANTINE,
    TARGET_BACKUP,
    TARGET_SERVER,
)
from mc_server_dashboard_api.servers.application.integrity_sweep import IntegritySweep
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.servers.fakes import (
    FakeBackupArchiveStore,
    FakeBackupRepository,
    FakeServerRepository,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 9, 12, 0, tzinfo=dt.timezone.utc)
_COMMUNITY = CommunityId(uuid.uuid4())


def _server(server_id: ServerId) -> Server:
    return Server(
        id=server_id,
        community_id=_COMMUNITY,
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _backup(server_id: ServerId, storage_ref: str, *, health: BackupHealth) -> Backup:
    return Backup(
        id=BackupId.new(),
        server_id=server_id,
        storage_ref=storage_ref,
        size_bytes=None,
        source=BackupSource.UPLOADED,
        health=health,
        created_by=None,
        created_at=_NOW,
    )


def _wire(
    *, servers: list[Server], backups: list[Backup]
) -> tuple[
    IntegritySweep, FakeUnitOfWork, FakeBackupArchiveStore, RecordingAuditRecorder
]:
    server_repo = FakeServerRepository()
    for server in servers:
        server_repo.seed(server)
    backup_repo = FakeBackupRepository()
    for backup in backups:
        backup_repo.seed(backup)
    uow = FakeUnitOfWork(servers=server_repo, backups=backup_repo)
    store = FakeBackupArchiveStore()
    for backup in backups:
        store.archives.add(backup.storage_ref)
    audit = RecordingAuditRecorder()
    sweep = IntegritySweep(uow=uow, backup_store=store, audit=audit)
    return sweep, uow, store, audit


async def test_classifies_mixed_backups_into_the_health_column() -> None:
    sid = ServerId.new()
    healthy = _backup(sid, "good", health=BackupHealth.UNKNOWN)
    corrupt = _backup(sid, "bad", health=BackupHealth.UNKNOWN)
    sweep, uow, store, _audit = _wire(
        servers=[_server(sid)], backups=[healthy, corrupt]
    )
    store.corrupt_refs.add("bad")

    summary = await sweep()

    assert uow.backups.by_id[healthy.id].health is BackupHealth.HEALTHY
    assert uow.backups.by_id[corrupt.id].health is BackupHealth.QUARANTINED
    assert summary.servers_scanned == 1
    assert summary.backups_healthy == 1
    assert summary.backups_quarantined == 1


async def test_quarantine_writes_an_audit_entry() -> None:
    sid = ServerId.new()
    corrupt = _backup(sid, "bad", health=BackupHealth.UNKNOWN)
    sweep, _uow, store, audit = _wire(servers=[_server(sid)], backups=[corrupt])
    store.corrupt_refs.add("bad")

    await sweep()

    quarantines = [e for e in audit.events if e.operation == BACKUP_QUARANTINE]
    assert len(quarantines) == 1
    assert quarantines[0].target_type == TARGET_BACKUP
    assert quarantines[0].target_id == corrupt.id.value


async def test_healthy_backup_is_not_audited() -> None:
    sid = ServerId.new()
    healthy = _backup(sid, "good", health=BackupHealth.UNKNOWN)
    sweep, _uow, _store, audit = _wire(servers=[_server(sid)], backups=[healthy])

    await sweep()

    assert not [e for e in audit.events if e.operation == BACKUP_QUARANTINE]


async def test_corrupt_snapshot_is_flagged_and_audited() -> None:
    sid = ServerId.new()
    sweep, _uow, store, audit = _wire(servers=[_server(sid)], backups=[])
    store.current_corrupt[sid] = 2  # a published-then-torn snapshot

    summary = await sweep()

    assert summary.snapshots_scanned == 1
    assert summary.snapshots_flagged == 1
    flags = [e for e in audit.events if e.operation == SNAPSHOT_QUARANTINE]
    assert len(flags) == 1
    assert flags[0].target_type == TARGET_SERVER
    assert flags[0].target_id == sid.value


async def test_healthy_snapshot_is_not_flagged() -> None:
    sid = ServerId.new()
    sweep, _uow, store, audit = _wire(servers=[_server(sid)], backups=[])
    store.current_corrupt[sid] = 0  # published, healthy

    summary = await sweep()

    assert summary.snapshots_scanned == 1
    assert summary.snapshots_flagged == 0
    assert not [e for e in audit.events if e.operation == SNAPSHOT_QUARANTINE]


async def test_unpublished_server_snapshot_is_skipped() -> None:
    sid = ServerId.new()
    sweep, _uow, _store, _audit = _wire(servers=[_server(sid)], backups=[])
    # No entry in current_corrupt -> the seam returns None (no published snapshot).

    summary = await sweep()

    assert summary.snapshots_scanned == 0
    assert summary.snapshots_flagged == 0


async def test_rerunning_is_idempotent() -> None:
    sid = ServerId.new()
    healthy = _backup(sid, "good", health=BackupHealth.UNKNOWN)
    corrupt = _backup(sid, "bad", health=BackupHealth.UNKNOWN)
    sweep, uow, store, _audit = _wire(
        servers=[_server(sid)], backups=[healthy, corrupt]
    )
    store.corrupt_refs.add("bad")
    store.current_corrupt[sid] = 1

    first = await sweep()
    second = await sweep()

    assert uow.backups.by_id[healthy.id].health is BackupHealth.HEALTHY
    assert uow.backups.by_id[corrupt.id].health is BackupHealth.QUARANTINED
    assert (
        first.servers_scanned,
        first.backups_healthy,
        first.backups_quarantined,
    ) == (
        second.servers_scanned,
        second.backups_healthy,
        second.backups_quarantined,
    )
    assert (first.snapshots_scanned, first.snapshots_flagged) == (
        second.snapshots_scanned,
        second.snapshots_flagged,
    )
