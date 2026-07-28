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
import logging
import uuid

import pytest

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
from mc_server_dashboard_api.servers.domain.errors import (
    BackupStorageUnavailableError,
)
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


async def test_dangling_backup_row_is_quarantined_and_counted() -> None:
    """A backup row whose archive is missing (crash-window dangling row) is
    marked QUARANTINED, counted as ``backups_dangling`` in the summary, and
    produces an audit entry — mirroring the lazy size backfill's handling."""
    sid = ServerId.new()
    dangling = _backup(sid, "gone", health=BackupHealth.UNKNOWN)
    sweep, uow, store, audit = _wire(servers=[_server(sid)], backups=[dangling])
    # Remove the archive so check_backup_health raises BackupNotFoundError.
    store.archives.discard("gone")

    summary = await sweep()

    assert uow.backups.by_id[dangling.id].health is BackupHealth.QUARANTINED
    assert summary.backups_dangling == 1
    assert summary.backups_quarantined == 0
    assert summary.backups_healthy == 0
    quarantines = [e for e in audit.events if e.operation == BACKUP_QUARANTINE]
    assert len(quarantines) == 1
    assert quarantines[0].target_id == dangling.id.value


async def test_unreadable_archive_is_quarantined_and_counted_separately() -> None:
    """A backup whose archive cannot be streamed back in full (issue #2371) is
    QUARANTINED with the same audit entry, but counted as ``backups_unreadable`` —
    the summary tells "the bytes are gone" from "the world is corrupt"."""

    sid = ServerId.new()
    unreadable = _backup(sid, "torn", health=BackupHealth.HEALTHY)
    sweep, uow, store, audit = _wire(servers=[_server(sid)], backups=[unreadable])
    store.unreadable_refs.add("torn")

    summary = await sweep()

    assert uow.backups.by_id[unreadable.id].health is BackupHealth.QUARANTINED
    assert summary.backups_unreadable == 1
    assert summary.backups_quarantined == 0
    assert summary.backups_healthy == 0
    quarantines = [e for e in audit.events if e.operation == BACKUP_QUARANTINE]
    assert len(quarantines) == 1
    assert quarantines[0].target_type == TARGET_BACKUP
    assert quarantines[0].target_id == unreadable.id.value


async def test_unreadable_archive_does_not_abort_remaining_backups() -> None:
    """One unreadable archive must not stop the pass: the rest of the server's
    backups still get classified."""

    sid = ServerId.new()
    unreadable = _backup(sid, "torn", health=BackupHealth.UNKNOWN)
    healthy = _backup(sid, "ok", health=BackupHealth.UNKNOWN)
    sweep, uow, store, _audit = _wire(
        servers=[_server(sid)], backups=[unreadable, healthy]
    )
    store.unreadable_refs.add("torn")

    summary = await sweep()

    assert uow.backups.by_id[unreadable.id].health is BackupHealth.QUARANTINED
    assert uow.backups.by_id[healthy.id].health is BackupHealth.HEALTHY
    assert summary.backups_unreadable == 1
    assert summary.backups_healthy == 1


async def test_store_outage_during_the_probe_never_quarantines() -> None:
    """A backend outage says nothing about an archive (issue #2371). It surfaces as
    the availability failure it is, leaving the health column untouched — a sweep
    run mid-outage must not condemn the deployment's backups."""

    sid = ServerId.new()
    backup = _backup(sid, "fine", health=BackupHealth.HEALTHY)
    sweep, uow, store, audit = _wire(servers=[_server(sid)], backups=[backup])
    store.unavailable_refs.add("fine")

    with pytest.raises(BackupStorageUnavailableError):
        await sweep()

    assert uow.backups.by_id[backup.id].health is BackupHealth.HEALTHY
    assert [e for e in audit.events if e.operation == BACKUP_QUARANTINE] == []


async def test_store_outage_names_the_backup_the_pass_died_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The operator has to be able to resume from a known point, so the abort logs
    WHICH backup it stopped at rather than only that it stopped (issue #2371)."""

    sid = ServerId.new()
    backup = _backup(sid, "fine", health=BackupHealth.UNKNOWN)
    sweep, _uow, store, _audit = _wire(servers=[_server(sid)], backups=[backup])
    store.unavailable_refs.add("fine")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(BackupStorageUnavailableError):
            await sweep()

    assert str(backup.id.value) in caplog.text
    assert str(sid.value) in caplog.text


async def test_dangling_row_does_not_abort_remaining_backups() -> None:
    """A dangling row must not propagate and abort the sweep — the healthy
    backup on the same server (and other servers) must still be checked."""
    s1 = ServerId.new()
    s2 = ServerId.new()
    dangling = _backup(s1, "gone", health=BackupHealth.UNKNOWN)
    healthy = _backup(s1, "ok", health=BackupHealth.UNKNOWN)
    other = _backup(s2, "also-ok", health=BackupHealth.UNKNOWN)
    sweep, uow, store, _audit = _wire(
        servers=[_server(s1), _server(s2)],
        backups=[dangling, healthy, other],
    )
    store.archives.discard("gone")

    summary = await sweep()

    assert uow.backups.by_id[dangling.id].health is BackupHealth.QUARANTINED
    assert uow.backups.by_id[healthy.id].health is BackupHealth.HEALTHY
    assert uow.backups.by_id[other.id].health is BackupHealth.HEALTHY
    assert summary.backups_dangling == 1
    assert summary.backups_healthy == 2
    assert summary.servers_scanned == 2


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
