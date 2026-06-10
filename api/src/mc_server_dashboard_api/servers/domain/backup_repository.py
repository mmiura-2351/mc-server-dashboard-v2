"""Persistence Port for backup metadata (DATABASE.md Section 8).

The ``BackupRepository`` the backup use cases depend on; a concrete
async-SQLAlchemy adapter implements it on the unit-of-work's session. Lookups
return ``None`` when absent rather than raising, so callers decide policy
(mirroring :class:`ServerRepository`).
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupStatistics,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


class BackupRepository(abc.ABC):
    """Port: persistence for :class:`Backup` metadata rows."""

    @abc.abstractmethod
    async def add(self, backup: Backup) -> None:
        """Stage a new backup row for persistence within the current transaction."""

    @abc.abstractmethod
    async def get_by_id(self, backup_id: BackupId) -> Backup | None:
        """Return the backup with ``backup_id``, or ``None`` if absent."""

    @abc.abstractmethod
    async def list_for_server(self, server_id: ServerId) -> list[Backup]:
        """Return a server's backups newest-first (the ``backup:read`` listing).

        Community scoping is enforced by the caller, which loads the (community-
        checked) server before listing; this is keyed by ``server_id`` only.
        Ordered by ``created_at`` descending, backed by the ``(server_id,
        created_at)`` index (DATABASE.md Section 8).
        """

    @abc.abstractmethod
    async def delete(self, backup_id: BackupId) -> None:
        """Delete the backup row (the archive bytes are removed separately)."""

    @abc.abstractmethod
    async def update_health(self, backup_id: BackupId, health: BackupHealth) -> None:
        """Set an existing backup's structural health (issue #743).

        Used by the restore gate to mark a backup ``QUARANTINED`` once a check
        found its contents corrupt — on a refused restore (corrupt, no force) and
        on a forced restore of a known-corrupt backup. A missing id is a no-op
        (the caller has already loaded the row, so this is staged within the same
        unit of work).
        """

    @abc.abstractmethod
    async def global_statistics(self) -> BackupStatistics:
        """Aggregate backup usage across the whole platform (issue #281).

        The platform-admin variant: count, summed *known* ``size_bytes``, the
        count of NULL-size (legacy) rows, and the newest/oldest ``created_at``
        across every server's backups. A single aggregate query, not a row scan.
        """
