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
from collections.abc import AsyncIterator

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
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        storage_ref: str,
        force: bool = False,
    ) -> int:
        """Atomically republish an archive into ``current/`` (FR-BAK-4, issue #743).

        Runs the restore-direction integrity gate over the extracted archive. A
        backup predating the create gate (#749) or an uploaded one may be
        structurally corrupt; by default (``force=False``) a corrupt archive is
        refused with :class:`BackupCorruptError` and ``current`` is untouched. With
        ``force=True`` the operator override publishes the corrupt archive anyway
        (better a deliberate corrupt restore than none, #703).

        Returns the corrupt region-file count of the published working set (``0``
        when healthy; always ``0`` without ``force``, since a corrupt one raises)
        so the use case can quarantine + audit a forced corrupt restore. The
        application enforces the stop precondition; Storage enforces atomicity.
        Raises :class:`BackupNotFoundError` for an unknown ref.
        """

    @abc.abstractmethod
    async def check_backup_health(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> int:
        """Extract an archive and structurally fsck it (the sweep probe, issue #744).

        Read-only: extracts the archive into throwaway staging, walks it for corrupt
        ``.mca`` region files (issue #738), and discards the staging — ``current`` is
        never touched. Returns the corrupt region-file count (``0`` when healthy) so
        the sweep persists ``HEALTHY`` / ``QUARANTINED`` on the backup row. Raises
        :class:`BackupNotFoundError` for an unknown ref.
        """

    @abc.abstractmethod
    async def check_current_health(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> int | None:
        """Structurally fsck the on-disk authoritative snapshot (the sweep, issue #744).

        Read-only: walks ``current/`` for corrupt ``.mca`` region files (issue #738)
        in place — a published snapshot is immutable/quiesced, so no staging is
        needed and ``current`` is never mutated. Returns the corrupt region-file
        count (``0`` when healthy), or ``None`` when no snapshot has been published
        (nothing to fsck) so the sweep skips the server's snapshot without erroring.
        """

    @abc.abstractmethod
    async def delete(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        """Remove an archive. Idempotent (a missing archive is a no-op)."""

    @abc.abstractmethod
    def open(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> AsyncIterator[bytes]:
        """Open a read stream over an archive in its native format (issue #281).

        Streams the stored bytes verbatim (no recompression) for download. Raises
        :class:`BackupNotFoundError` for an unknown ref.
        """

    @abc.abstractmethod
    async def store(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        stream: AsyncIterator[bytes],
    ) -> str:
        """Store an uploaded archive verbatim, returning its ref (issue #281).

        The caller has already validated the archive; this only stores the bytes
        and returns the new ref (restorable via :meth:`restore`).
        """

    @abc.abstractmethod
    async def size(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> int:
        """Return an archive's size in bytes (issue #281).

        Raises :class:`BackupNotFoundError` for an unknown ref.
        """
