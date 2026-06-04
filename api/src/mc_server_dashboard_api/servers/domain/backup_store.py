"""The servers-side backup-archive seam (the backup layer's view of Storage).

The backup use cases must archive / restore / delete a server's working set —
all Storage concerns (STORAGE.md Section 3.3). The servers domain and application
may not construct a storage *adapter*, so they depend on this narrow Port; the
wiring binds it to a storage adapter that drives the real ``BackupStore`` slice
(mirroring the file layer's :class:`FileStore` seam).

The Port speaks the servers domain's own ids and a plain ``str`` archive
reference (the ``BackupKey`` value), and raises the servers backup error
(:class:`BackupNotFoundError`); the adapter translates the storage
``NotFoundError`` at the seam, so no storage type crosses into the application
layer.
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)


class BackupArchiveStore(abc.ABC):
    """Port: the backup layer's seam to the authoritative-copy archive store."""

    @abc.abstractmethod
    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> str:
        """Archive the authoritative ``current/`` and return the archive ref (FR-BAK-1).

        The stopped path and the tail of the running path both call this: Storage
        only ever archives the authoritative copy (Section 6.9). Raises
        :class:`BackupNotFoundError` if nothing is published to archive.
        """

    @abc.abstractmethod
    async def restore(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        """Atomically republish an archive into ``current/`` (FR-BAK-4).

        The application enforces the stop precondition; Storage enforces
        atomicity. Raises :class:`BackupNotFoundError` for an unknown ref.
        """

    @abc.abstractmethod
    async def delete(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        """Remove an archive. Idempotent (a missing archive is a no-op)."""
