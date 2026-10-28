"""One-shot fsck/quarantine sweep of existing backups & snapshots (issue #744).

The create/restore integrity gates (#749/#743) only check *new* artifacts; the
2026-06-09 destructive test left corrupt snapshots and backups that predate them.
This sweep is the explicitly-invoked maintenance pass that re-checks the existing
artifacts of every server (or a single server) and persists/surfaces the result:

- **Backups** (DB-tracked): for each backup row, run the read-only
  ``check_backup_health`` Storage probe and write the verdict to the ``health``
  column via ``update_health`` (#743) — ``HEALTHY`` for a sound archive,
  ``QUARANTINED`` for a structurally corrupt one, ``UNREADABLE`` for one whose
  bytes cannot be read back or are missing (#2374). What the probe does is
  backend-specific: the fs adapter extracts the archive under the decompressed-byte
  cap and fscks the region files; the object adapter streams the stored archive end
  to end and proves the store can still produce it (#2371). Every row is re-read
  whatever its current health, so no verdict sticks past a sweep that contradicts
  it: a ``QUARANTINED`` or ``UNREADABLE`` row whose archive now reads back sound
  returns to ``HEALTHY``, and rows quarantined before ``UNREADABLE`` existed are
  re-classified.
- **Snapshots** (no DB row): fsck the published ``current`` world in place and
  **log/audit** its health — there is no snapshot model to update, so surfacing is
  report/audit-only. This half is fs-only: the object backend materializes no
  working set to walk (the #926 limitation), so it examines nothing and the sweep
  reports its snapshots as **not examined** rather than counting them scanned and
  clean (#2377).

Backup creation commits the metadata row before it writes the archive, so a row
can be visible while its archive does not exist yet (issue #3220). The backup half
checks each backup under the server's lifecycle lock, which ``CreateBackup`` holds
across that row commit and archive write: the sweep waits out a publication in
flight instead of condemning its archive as missing. The lock is taken per backup,
not per server, so a heavy pass never blocks a start, restore or backup for more
than one archive check; inside it the row is re-read, since it may have been
deleted since the listing. A backup whose lock stays busy past the lock's wait
budget is skipped for this run — no verdict, no audit — and counted as
``backups_skipped_busy`` so the operator knows to re-run the sweep; the server's
other backups and its snapshot are still checked. The snapshot half needs no
lock: ``current`` is published by an atomic swap and the fsck holds a reader lease
on the snapshot it walks (#1702), so it never sees a half-published snapshot.

A quarantined backup, an unreadable backup and a flagged snapshot each emit an
audit entry. The pass is
heavy (a full read per archive), so it logs per-backup progress. It is idempotent:
re-running re-checks the same bytes and yields the same classification, with no
on-disk or summary state that drifts.

This is an application use case, not an edge route: it owns its own post-commit
audit point (the sweep has no HTTP request behind it). The actor is the operator
who invoked it (``None`` when run headless).
"""

from __future__ import annotations

import enum
import logging
import uuid
from collections import Counter
from dataclasses import dataclass

from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.operations import (
    BACKUP_MARK_UNREADABLE,
    BACKUP_QUARANTINE,
    SNAPSHOT_QUARANTINE,
    TARGET_BACKUP,
    TARGET_SERVER,
)
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.servers.domain.backup import Backup, BackupHealth
from mc_server_dashboard_api.servers.domain.backup_store import (
    BackupArchiveStore,
    SnapshotScan,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    BackupNotFoundError,
    BackupStorageUnavailableError,
    BackupUnreadableError,
    ServerBusyError,
)
from mc_server_dashboard_api.servers.domain.lifecycle_lock import (
    LifecycleLock,
    NullLifecycleLock,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SweepSummary:
    """Counts emitted by a sweep run (servers, backups, snapshots).

    ``snapshots_scanned`` counts only servers whose ``current`` was published AND
    actually walked (an unpublished server has nothing to fsck and is skipped);
    ``snapshots_flagged`` is how many of those were structurally corrupt.

    ``snapshots_not_examined`` counts servers whose snapshot the backend never
    looked at (issue #2377) — on the object backend that is every server, published
    or not, since it reads nothing that would tell the two apart. Keeping it out of
    ``snapshots_scanned`` is the point: "scanned: N, flagged: 0" is a verdict, and
    a backend that examines nothing must not be able to produce one.

    ``backups_skipped_busy`` counts backups left unexamined because another
    lifecycle operation held their server's lock past its wait budget (issue
    #3220); they got no verdict, and re-running the sweep checks them.

    The three condemned-backup counts are kept apart because an operator acts on
    them differently: ``backups_quarantined`` is a structurally corrupt world
    (``QUARANTINED``, still force-restorable), ``backups_unreadable`` is an archive
    the store can no longer produce (#2371) and ``backups_dangling`` is a row with
    no archive at all. The last two are both "the bytes are gone" rather than "the
    world is corrupt" and both write ``UNREADABLE`` (#2374); they stay counted apart
    because a missing object and a damaged one point at different storage faults.
    """

    servers_scanned: int
    backups_healthy: int
    backups_quarantined: int
    backups_unreadable: int
    backups_dangling: int
    backups_skipped_busy: int
    snapshots_scanned: int
    snapshots_flagged: int
    snapshots_not_examined: int


class _BackupOutcome(enum.Enum):
    """What the sweep did with one backup row; tallied into the summary."""

    HEALTHY = enum.auto()
    QUARANTINED = enum.auto()
    UNREADABLE = enum.auto()
    DANGLING = enum.auto()
    SKIPPED_BUSY = enum.auto()


@dataclass(frozen=True)
class IntegritySweep:
    """Re-fsck and quarantine existing backups & snapshots (issue #744).

    Enumerates servers via the repository (all servers, or one when ``server_id`` is
    passed), and for each: re-checks its backups into the ``health`` column and
    fscks its published snapshot (audit/log only). ``actor_id`` attributes the audit
    entries to the operator who ran the command. Each backup check runs under its
    server's ``lifecycle_lock`` (issue #3220).
    """

    uow: UnitOfWork
    backup_store: BackupArchiveStore
    audit: AuditRecorder
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self, *, server_id: ServerId | None = None, actor_id: uuid.UUID | None = None
    ) -> SweepSummary:
        servers = await self._servers_to_scan(server_id)
        backups = Counter[_BackupOutcome]()
        snapshots_scanned = 0
        snapshots_flagged = 0
        snapshots_not_examined = 0
        for server in servers:
            _LOG.info("integrity sweep: scanning server %s", server.id.value)
            backups.update(await self._sweep_backups(server, actor_id))
            scanned, flagged, not_examined = await self._sweep_snapshot(
                server, actor_id
            )
            snapshots_scanned += scanned
            snapshots_flagged += flagged
            snapshots_not_examined += not_examined
        summary = SweepSummary(
            servers_scanned=len(servers),
            backups_healthy=backups[_BackupOutcome.HEALTHY],
            backups_quarantined=backups[_BackupOutcome.QUARANTINED],
            backups_unreadable=backups[_BackupOutcome.UNREADABLE],
            backups_dangling=backups[_BackupOutcome.DANGLING],
            backups_skipped_busy=backups[_BackupOutcome.SKIPPED_BUSY],
            snapshots_scanned=snapshots_scanned,
            snapshots_flagged=snapshots_flagged,
            snapshots_not_examined=snapshots_not_examined,
        )
        _LOG.info(
            "integrity sweep done: %d servers, %d backups healthy, %d quarantined "
            "(corrupt world), %d unreadable (archive bytes unproducible), "
            "%d dangling, %d skipped busy, %d snapshots scanned, %d flagged, "
            "%d not examined (backend walks no snapshot at rest)",
            summary.servers_scanned,
            summary.backups_healthy,
            summary.backups_quarantined,
            summary.backups_unreadable,
            summary.backups_dangling,
            summary.backups_skipped_busy,
            summary.snapshots_scanned,
            summary.snapshots_flagged,
            summary.snapshots_not_examined,
        )
        return summary

    async def _servers_to_scan(self, server_id: ServerId | None) -> list[Server]:
        async with self.uow:
            if server_id is None:
                return await self.uow.servers.list_all()
            server = await self.uow.servers.get_by_id(server_id)
            return [server] if server is not None else []

    async def _sweep_backups(
        self, server: Server, actor_id: uuid.UUID | None
    ) -> Counter[_BackupOutcome]:
        async with self.uow:
            backups = await self.uow.backups.list_for_server(server.id)
        outcomes = Counter[_BackupOutcome]()
        for listed in backups:
            # The lifecycle lock per backup (issue #3220): ``CreateBackup`` holds it
            # from its row commit to its archive write, so under it a row is never
            # seen mid-publication. Per backup rather than per server, so the lock
            # is never held across more than one archive check.
            try:
                async with self.lifecycle_lock.hold(server.id):
                    # Re-read under the lock: the row may have been deleted (or
                    # re-judged) since the listing.
                    async with self.uow:
                        backup = await self.uow.backups.get_by_id(listed.id)
                    if backup is None:
                        _LOG.info(
                            "integrity sweep: backup %s was deleted during the "
                            "sweep; nothing to check",
                            listed.id.value,
                        )
                        continue
                    outcomes[await self._check_backup(server, backup, actor_id)] += 1
            except ServerBusyError:
                # Another lifecycle operation (a backup being published, a restore,
                # ...) held the server past the lock's wait budget; this row may be
                # mid-publication, so no verdict is safe now. Skip it rather than
                # fail the pass; a re-run checks it.
                _LOG.info(
                    "integrity sweep: backup %s skipped, server %s is busy (another "
                    "lifecycle operation holds it); re-run the sweep to check it",
                    listed.id.value,
                    server.id.value,
                )
                outcomes[_BackupOutcome.SKIPPED_BUSY] += 1
        return outcomes

    async def _check_backup(
        self, server: Server, backup: Backup, actor_id: uuid.UUID | None
    ) -> _BackupOutcome:
        """Probe one backup's archive and persist (and audit) its verdict."""

        try:
            corrupt_count = await self.backup_store.check_backup_health(
                community_id=server.community_id,
                server_id=server.id,
                storage_ref=backup.storage_ref,
            )
        except BackupNotFoundError:
            # No archive at all: nothing to restore, override or not, so it
            # gets the same verdict as bytes the store cannot produce (#2374).
            _LOG.warning(
                "integrity sweep: backup %s has no archive (dangling row); "
                "marking unreadable",
                backup.id.value,
            )
            await self._condemn(server, backup, BackupHealth.UNREADABLE, actor_id)
            return _BackupOutcome.DANGLING
        except BackupUnreadableError:
            # The archive exists but the store cannot produce its bytes (#2371):
            # UNREADABLE, not QUARANTINED (#2374) — "the bytes are gone" cannot
            # be force-restored the way a corrupt world can, and it points at
            # storage damage rather than at the world.
            _LOG.warning(
                "integrity sweep: backup %s archive could not be read back "
                "(the store cannot produce its bytes); marking unreadable",
                backup.id.value,
            )
            await self._condemn(server, backup, BackupHealth.UNREADABLE, actor_id)
            return _BackupOutcome.UNREADABLE
        except BackupStorageUnavailableError:
            # An outage is no verdict about THIS archive (#2371), and re-running
            # the pass against a down store would quarantine nothing correctly.
            # Stop instead of misclassifying, naming the row the pass died on so
            # the operator can resume from a known point; the CLI turns this into
            # a clean failure rather than a traceback.
            # ``exc_info`` is load-bearing, not decoration: the object client
            # translates ANY body-read failure to a storage error, so the
            # aiohttp/botocore error naming the actual fault survives only in the
            # ``__cause__`` chain. Without the traceback here an operator sees
            # "the store could not serve the read" at every surface and never
            # why — which is what would make that broad catch a black hole.
            _LOG.error(
                "integrity sweep: aborting at backup %s on server %s — the "
                "object store could not serve the archive read. No verdict was "
                "written for it; re-run the sweep once the store is healthy.",
                backup.id.value,
                server.id.value,
                exc_info=True,
            )
            raise
        health = BackupHealth.QUARANTINED if corrupt_count else BackupHealth.HEALTHY
        _LOG.info(
            "integrity sweep: backup %s -> %s (%d corrupt region files)",
            backup.id.value,
            health.value,
            corrupt_count,
        )
        if health is BackupHealth.QUARANTINED:
            await self._condemn(server, backup, health, actor_id)
            return _BackupOutcome.QUARANTINED
        async with self.uow:
            await self.uow.backups.update_health(backup.id, health)
            await self.uow.commit()
        return _BackupOutcome.HEALTHY

    async def _condemn(
        self,
        server: Server,
        backup: Backup,
        health: BackupHealth,
        actor_id: uuid.UUID | None,
    ) -> None:
        """Persist a QUARANTINED / UNREADABLE verdict on a backup row and audit it.

        Every reason a backup is condemned — a corrupt world, a missing archive, an
        archive the store cannot produce — goes through this one write, so the
        verdict and its audit entry cannot drift apart. The audit operation follows
        the verdict, keeping the two remedies queryable apart in the trail.
        """

        async with self.uow:
            await self.uow.backups.update_health(backup.id, health)
            await self.uow.commit()
        operation = (
            BACKUP_MARK_UNREADABLE
            if health is BackupHealth.UNREADABLE
            else BACKUP_QUARANTINE
        )
        await self._audit_backup_verdict(
            server.community_id, backup, operation, actor_id
        )

    async def _sweep_snapshot(
        self, server: Server, actor_id: uuid.UUID | None
    ) -> tuple[int, int, int]:
        """Fsck one server's published snapshot -> (scanned, flagged, not examined)."""

        outcome = await self.backup_store.check_current_health(
            community_id=server.community_id, server_id=server.id
        )
        if outcome is SnapshotScan.NOT_PUBLISHED:
            return 0, 0, 0  # no published snapshot: nothing to fsck.
        if outcome is SnapshotScan.NOT_EXAMINED:
            # The backend walked nothing (issue #2377). Say so instead of logging a
            # health verdict it never established: a "healthy" line here is exactly
            # the vacuous reassurance this counter exists to withdraw.
            _LOG.info(
                "integrity sweep: snapshot for server %s not examined (this storage "
                "backend does not fsck published snapshots at rest)",
                server.id.value,
            )
            return 0, 0, 1
        if outcome == 0:
            _LOG.info(
                "integrity sweep: snapshot for server %s healthy", server.id.value
            )
            return 1, 0, 0
        _LOG.warning(
            "integrity sweep: snapshot for server %s corrupt (%d region files)",
            server.id.value,
            outcome,
        )
        await self._audit_snapshot_flag(server, actor_id)
        return 1, 1, 0

    async def _audit_backup_verdict(
        self,
        community_id: CommunityId,
        backup: Backup,
        operation: str,
        actor_id: uuid.UUID | None,
    ) -> None:
        await self.audit.record(
            AuditEvent(
                operation=operation,
                outcome=Outcome.SUCCESS,
                actor_id=actor_id,
                community_id=community_id.value,
                target_type=TARGET_BACKUP,
                target_id=backup.id.value,
            )
        )

    async def _audit_snapshot_flag(
        self, server: Server, actor_id: uuid.UUID | None
    ) -> None:
        # Snapshots are filesystem-only (no DB id), so the audit targets the server
        # whose published ``current`` was found corrupt.
        await self.audit.record(
            AuditEvent(
                operation=SNAPSHOT_QUARANTINE,
                outcome=Outcome.SUCCESS,
                actor_id=actor_id,
                community_id=server.community_id.value,
                target_type=TARGET_SERVER,
                target_id=server.id.value,
            )
        )
