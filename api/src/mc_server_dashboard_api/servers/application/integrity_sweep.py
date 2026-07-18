"""One-shot fsck/quarantine sweep of existing backups & snapshots (issue #744).

The create/restore integrity gates (#749/#743) only check *new* artifacts; the
2026-06-09 destructive test left corrupt snapshots and backups that predate them.
This sweep is the explicitly-invoked maintenance pass that re-checks the existing
artifacts of every server (or a single server) and persists/surfaces the result:

- **Backups** (DB-tracked): for each backup row, extract-and-fsck the archive
  under the decompressed-byte cap (the ``check_backup_health`` Storage probe,
  read-only) and write the verdict to the ``health`` column via ``update_health``
  (#743) — ``HEALTHY`` for a sound archive, ``QUARANTINED`` for a corrupt one.
- **Snapshots** (filesystem-only, no DB row): fsck the published ``current`` world
  in place and **log/audit** its health — there is no snapshot model to update, so
  surfacing is report/audit-only.

A quarantined backup and a flagged snapshot each emit an audit entry. The pass is
heavy (an extract per archive), so it logs per-backup progress. It is idempotent:
re-running re-checks the same bytes and yields the same classification, with no
on-disk or summary state that drifts.

This is an application use case, not an edge route: it owns its own post-commit
audit point (the sweep has no HTTP request behind it). The actor is the operator
who invoked it (``None`` when run headless).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.operations import (
    BACKUP_QUARANTINE,
    SNAPSHOT_QUARANTINE,
    TARGET_BACKUP,
    TARGET_SERVER,
)
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.servers.domain.backup import Backup, BackupHealth
from mc_server_dashboard_api.servers.domain.backup_store import BackupArchiveStore
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import BackupNotFoundError
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SweepSummary:
    """Counts emitted by a sweep run (servers, backups, snapshots).

    ``snapshots_scanned`` counts only servers whose ``current`` was published (an
    unpublished server has nothing to fsck and is skipped); ``snapshots_flagged``
    is how many of those were structurally corrupt.
    """

    servers_scanned: int
    backups_healthy: int
    backups_quarantined: int
    backups_dangling: int
    snapshots_scanned: int
    snapshots_flagged: int


@dataclass(frozen=True)
class IntegritySweep:
    """Re-fsck and quarantine existing backups & snapshots (issue #744).

    Enumerates servers via the repository (all servers, or one when ``server_id`` is
    passed), and for each: re-checks its backups into the ``health`` column and
    fscks its published snapshot (audit/log only). ``actor_id`` attributes the audit
    entries to the operator who ran the command.
    """

    uow: UnitOfWork
    backup_store: BackupArchiveStore
    audit: AuditRecorder

    async def __call__(
        self, *, server_id: ServerId | None = None, actor_id: uuid.UUID | None = None
    ) -> SweepSummary:
        servers = await self._servers_to_scan(server_id)
        backups_healthy = 0
        backups_quarantined = 0
        backups_dangling = 0
        snapshots_scanned = 0
        snapshots_flagged = 0
        for server in servers:
            _LOG.info("integrity sweep: scanning server %s", server.id.value)
            healthy, quarantined, dangling = await self._sweep_backups(server, actor_id)
            backups_healthy += healthy
            backups_quarantined += quarantined
            backups_dangling += dangling
            scanned, flagged = await self._sweep_snapshot(server, actor_id)
            snapshots_scanned += scanned
            snapshots_flagged += flagged
        summary = SweepSummary(
            servers_scanned=len(servers),
            backups_healthy=backups_healthy,
            backups_quarantined=backups_quarantined,
            backups_dangling=backups_dangling,
            snapshots_scanned=snapshots_scanned,
            snapshots_flagged=snapshots_flagged,
        )
        _LOG.info(
            "integrity sweep done: %d servers, %d backups healthy, %d quarantined, "
            "%d dangling, %d snapshots scanned, %d flagged",
            summary.servers_scanned,
            summary.backups_healthy,
            summary.backups_quarantined,
            summary.backups_dangling,
            summary.snapshots_scanned,
            summary.snapshots_flagged,
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
    ) -> tuple[int, int, int]:
        async with self.uow:
            backups = await self.uow.backups.list_for_server(server.id)
        healthy = 0
        quarantined = 0
        dangling = 0
        for backup in backups:
            try:
                corrupt_count = await self.backup_store.check_backup_health(
                    community_id=server.community_id,
                    server_id=server.id,
                    storage_ref=backup.storage_ref,
                )
            except BackupNotFoundError:
                _LOG.warning(
                    "integrity sweep: backup %s has no archive (dangling row); "
                    "quarantining",
                    backup.id.value,
                )
                async with self.uow:
                    await self.uow.backups.update_health(
                        backup.id, BackupHealth.QUARANTINED
                    )
                    await self.uow.commit()
                dangling += 1
                await self._audit_backup_quarantine(
                    server.community_id, backup, actor_id
                )
                continue
            health = BackupHealth.QUARANTINED if corrupt_count else BackupHealth.HEALTHY
            _LOG.info(
                "integrity sweep: backup %s -> %s (%d corrupt region files)",
                backup.id.value,
                health.value,
                corrupt_count,
            )
            async with self.uow:
                await self.uow.backups.update_health(backup.id, health)
                await self.uow.commit()
            if health is BackupHealth.QUARANTINED:
                quarantined += 1
                await self._audit_backup_quarantine(
                    server.community_id, backup, actor_id
                )
            else:
                healthy += 1
        return healthy, quarantined, dangling

    async def _sweep_snapshot(
        self, server: Server, actor_id: uuid.UUID | None
    ) -> tuple[int, int]:
        corrupt_count = await self.backup_store.check_current_health(
            community_id=server.community_id, server_id=server.id
        )
        if corrupt_count is None:
            return 0, 0  # no published snapshot: nothing to fsck.
        if corrupt_count == 0:
            _LOG.info(
                "integrity sweep: snapshot for server %s healthy", server.id.value
            )
            return 1, 0
        _LOG.warning(
            "integrity sweep: snapshot for server %s corrupt (%d region files)",
            server.id.value,
            corrupt_count,
        )
        await self._audit_snapshot_flag(server, actor_id)
        return 1, 1

    async def _audit_backup_quarantine(
        self, community_id: CommunityId, backup: Backup, actor_id: uuid.UUID | None
    ) -> None:
        await self.audit.record(
            AuditEvent(
                operation=BACKUP_QUARANTINE,
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
