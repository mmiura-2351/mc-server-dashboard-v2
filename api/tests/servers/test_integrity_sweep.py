"""Use-case tests for the one-shot fsck/quarantine sweep (issue #744).

Exercises :class:`IntegritySweep` against fakes (no DB, no real Storage), per
TESTING.md Section 4. The sweep enumerates every server, re-checks each backup
(extract-and-fsck) and persists ``HEALTHY`` / ``QUARANTINED`` / ``UNREADABLE`` on
the backup row,
fscks the published ``current`` snapshot (report/audit only — snapshots are
filesystem-only), audits each quarantine, and returns a summary. Re-running yields
the same classification (idempotent).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from mc_server_dashboard_api.audit.domain.operations import (
    BACKUP_MARK_UNREADABLE,
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
    ServerBusyError,
)
from mc_server_dashboard_api.servers.domain.lifecycle_lock import (
    LifecycleLock,
    NullLifecycleLock,
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
    FakeLifecycleLock,
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


class _BusyLifecycleLock(LifecycleLock):
    """A lifecycle lock another operation holds past the wait budget on the
    ``busy`` acquisitions (1-based): ``hold`` raises ``ServerBusyError`` for them,
    exactly as the real lock does once its bounded acquire gives up."""

    def __init__(self, busy: set[int]) -> None:
        self.busy = busy
        self.acquisitions = 0

    @contextlib.asynccontextmanager
    async def hold(self, server_id: ServerId) -> AsyncIterator[None]:
        self.acquisitions += 1
        if self.acquisitions in self.busy:
            raise ServerBusyError(str(server_id.value))
        yield


class _DeletingLifecycleLock(LifecycleLock):
    """A lifecycle lock whose first acquisition comes after a ``DeleteBackup``
    removed ``backup`` (archive first, then row) between the sweep's listing and
    its check."""

    def __init__(
        self, uow: FakeUnitOfWork, store: FakeBackupArchiveStore, backup: Backup
    ) -> None:
        self.uow = uow
        self.store = store
        self.backup = backup

    @contextlib.asynccontextmanager
    async def hold(self, server_id: ServerId) -> AsyncIterator[None]:
        self.store.archives.discard(self.backup.storage_ref)
        self.uow.backups.by_id.pop(self.backup.id, None)
        yield


def _wire(
    *,
    servers: list[Server],
    backups: list[Backup],
    lifecycle_lock: LifecycleLock = NullLifecycleLock(),
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
    sweep = IntegritySweep(
        uow=uow, backup_store=store, audit=audit, lifecycle_lock=lifecycle_lock
    )
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
    # No entry in current_corrupt -> NOT_PUBLISHED (nothing has been published).

    summary = await sweep()

    assert summary.snapshots_scanned == 0
    assert summary.snapshots_flagged == 0
    assert summary.snapshots_not_examined == 0


async def test_snapshot_on_an_unexamining_backend_is_not_counted_as_scanned() -> None:
    """A backend that examines no snapshot at rest must not inflate the scanned
    count (issue #2377).

    On the object backend nothing about a published snapshot is read, so "scanned:
    1, flagged: 0" would be a vacuous verdict an operator cannot distinguish from a
    real one. It is counted as *not examined* instead, and nothing is flagged.
    """

    sid = ServerId.new()
    sweep, _uow, store, audit = _wire(servers=[_server(sid)], backups=[])
    store.examines_snapshots = False

    summary = await sweep()

    assert summary.snapshots_scanned == 0
    assert summary.snapshots_flagged == 0
    assert summary.snapshots_not_examined == 1
    assert not [e for e in audit.events if e.operation == SNAPSHOT_QUARANTINE]


async def test_dangling_backup_row_is_marked_unreadable_and_counted() -> None:
    """A backup row whose archive is missing (crash-window dangling row) is
    marked UNREADABLE (issue #2374) — there are no bytes to restore, the same
    remedy as an archive the store cannot produce — counted as
    ``backups_dangling`` in the summary, and audited as such."""
    sid = ServerId.new()
    dangling = _backup(sid, "gone", health=BackupHealth.UNKNOWN)
    sweep, uow, store, audit = _wire(servers=[_server(sid)], backups=[dangling])
    # Remove the archive so check_backup_health raises BackupNotFoundError.
    store.archives.discard("gone")

    summary = await sweep()

    assert uow.backups.by_id[dangling.id].health is BackupHealth.UNREADABLE
    assert summary.backups_dangling == 1
    assert summary.backups_quarantined == 0
    assert summary.backups_healthy == 0
    marks = [e for e in audit.events if e.operation == BACKUP_MARK_UNREADABLE]
    assert len(marks) == 1
    assert marks[0].target_id == dangling.id.value
    assert not [e for e in audit.events if e.operation == BACKUP_QUARANTINE]


async def test_unreadable_archive_is_marked_unreadable_not_quarantined() -> None:
    """A backup whose archive cannot be streamed back in full (issue #2371) gets
    its own verdict, UNREADABLE (issue #2374), and its own audit operation — not
    the QUARANTINED a structurally corrupt world gets, because the remedies
    differ: a corrupt world can still be force-restored, missing bytes cannot."""

    sid = ServerId.new()
    unreadable = _backup(sid, "torn", health=BackupHealth.HEALTHY)
    sweep, uow, store, audit = _wire(servers=[_server(sid)], backups=[unreadable])
    store.unreadable_refs.add("torn")

    summary = await sweep()

    assert uow.backups.by_id[unreadable.id].health is BackupHealth.UNREADABLE
    assert summary.backups_unreadable == 1
    assert summary.backups_quarantined == 0
    assert summary.backups_healthy == 0
    marks = [e for e in audit.events if e.operation == BACKUP_MARK_UNREADABLE]
    assert len(marks) == 1
    assert marks[0].target_type == TARGET_BACKUP
    assert marks[0].target_id == unreadable.id.value
    assert not [e for e in audit.events if e.operation == BACKUP_QUARANTINE]


async def test_previously_quarantined_row_is_reclassified_by_the_next_sweep() -> None:
    """Rows quarantined before UNREADABLE existed mix both meanings (issue #2374);
    the owner accepted that only because the next sweep re-examines them. Pin that
    a QUARANTINED row is re-read, not skipped: an unproducible archive moves to
    UNREADABLE and a now-sound one to HEALTHY."""

    sid = ServerId.new()
    torn = _backup(sid, "torn", health=BackupHealth.QUARANTINED)
    sound = _backup(sid, "sound", health=BackupHealth.QUARANTINED)
    sweep, uow, store, _audit = _wire(servers=[_server(sid)], backups=[torn, sound])
    store.unreadable_refs.add("torn")

    await sweep()

    assert uow.backups.by_id[torn.id].health is BackupHealth.UNREADABLE
    assert uow.backups.by_id[sound.id].health is BackupHealth.HEALTHY


async def test_unreadable_verdict_is_revised_when_the_archive_reads_back() -> None:
    """UNREADABLE is not terminal: a later sweep that reads the archive back in
    full returns the row to HEALTHY (issue #2374, the revisability #2379 needs)."""

    sid = ServerId.new()
    backup = _backup(sid, "recovered", health=BackupHealth.UNREADABLE)
    sweep, uow, _store, audit = _wire(servers=[_server(sid)], backups=[backup])

    summary = await sweep()

    assert uow.backups.by_id[backup.id].health is BackupHealth.HEALTHY
    assert summary.backups_healthy == 1
    assert not [e for e in audit.events if e.operation == BACKUP_MARK_UNREADABLE]


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

    assert uow.backups.by_id[unreadable.id].health is BackupHealth.UNREADABLE
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
    assert [e for e in audit.events if e.operation == BACKUP_MARK_UNREADABLE] == []


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

    assert uow.backups.by_id[dangling.id].health is BackupHealth.UNREADABLE
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


async def test_dangling_row_is_still_condemned_under_the_lifecycle_lock() -> None:
    """Holding the lifecycle lock (issue #3220) only rules out a publication in
    flight; a row whose archive is missing with nothing publishing it is a real
    dangling row and still gets the UNREADABLE verdict and its audit."""

    sid = ServerId.new()
    dangling = _backup(sid, "gone", health=BackupHealth.HEALTHY)
    lock = FakeLifecycleLock()
    sweep, uow, store, audit = _wire(
        servers=[_server(sid)], backups=[dangling], lifecycle_lock=lock
    )
    store.archives.discard("gone")

    summary = await sweep()

    assert lock.events == [(sid, "acquire"), (sid, "release")]
    assert uow.backups.by_id[dangling.id].health is BackupHealth.UNREADABLE
    assert summary.backups_dangling == 1
    marks = [e for e in audit.events if e.operation == BACKUP_MARK_UNREADABLE]
    assert [e.target_id for e in marks] == [dangling.id.value]


async def test_busy_backup_is_skipped_while_the_rest_of_the_server_is_swept(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The lock is taken per backup (issue #3220). A backup whose lock stays held
    past the wait budget (a publication in flight, a restore, ...) is skipped for
    this run: no verdict, no audit, logged at INFO and counted apart. The server's
    other backups still get verdicts and its snapshot is still checked. Judging
    the row mid-publication is exactly the false "dangling" verdict the lock
    exists to prevent."""

    sid = ServerId.new()
    # Newest first, so the sweep reaches it on the first (busy) acquisition. It
    # would be condemned as dangling if it were examined.
    pending = replace(
        _backup(sid, "pending", health=BackupHealth.HEALTHY),
        created_at=_NOW + dt.timedelta(hours=1),
    )
    corrupt = _backup(sid, "bad", health=BackupHealth.UNKNOWN)
    sound = _backup(sid, "ok", health=BackupHealth.UNKNOWN)
    sweep, uow, store, audit = _wire(
        servers=[_server(sid)],
        backups=[pending, corrupt, sound],
        lifecycle_lock=_BusyLifecycleLock({1}),
    )
    store.archives.discard("pending")
    store.corrupt_refs.add("bad")
    store.current_corrupt[sid] = 1

    with caplog.at_level(logging.INFO):
        summary = await sweep()

    assert uow.backups.by_id[pending.id].health is BackupHealth.HEALTHY
    assert uow.backups.by_id[corrupt.id].health is BackupHealth.QUARANTINED
    assert uow.backups.by_id[sound.id].health is BackupHealth.HEALTHY
    assert [e.target_id for e in audit.events] == [corrupt.id.value, sid.value]
    assert summary.backups_skipped_busy == 1
    assert summary.backups_dangling == 0
    assert summary.backups_quarantined == 1
    assert summary.backups_healthy == 1
    assert summary.snapshots_flagged == 1
    skipped = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO
        and str(pending.id.value) in r.getMessage()
        and "busy" in r.getMessage()
    ]
    assert skipped


async def test_backup_deleted_before_its_lock_is_taken_gets_no_verdict() -> None:
    """The row is re-read under the lock (issue #3220): a backup deleted between
    the listing and the check is gone, not dangling, so it gets no verdict and no
    audit."""

    sid = ServerId.new()
    doomed = _backup(sid, "doomed", health=BackupHealth.HEALTHY)
    sweep, uow, store, audit = _wire(servers=[_server(sid)], backups=[doomed])
    sweep = replace(sweep, lifecycle_lock=_DeletingLifecycleLock(uow, store, doomed))

    summary = await sweep()

    assert doomed.id not in uow.backups.by_id
    assert audit.events == []
    assert summary.backups_dangling == 0
    assert summary.backups_healthy == 0
