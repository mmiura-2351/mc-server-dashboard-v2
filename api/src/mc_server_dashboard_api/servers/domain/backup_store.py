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
import enum
from collections.abc import AsyncIterator

from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)


class SnapshotScan(enum.Enum):
    """Why a snapshot fsck produced no corrupt-region count (issue #2377).

    :meth:`BackupArchiveStore.check_current_health` returns an ``int`` when it has a
    real verdict about a published snapshot; these are the two ways it has none, and
    they are kept apart because the sweep summary reports them differently.
    """

    NOT_PUBLISHED = "not_published"
    """No snapshot has been published for this server — there is nothing to fsck."""

    NOT_EXAMINED = "not_examined"
    """The backend examines no published snapshot at rest (the #926 limitation).

    The object backend has no local working set to walk, so it looks at nothing. It
    answers this for every server, published or not: it reads nothing that would
    tell the two apart.
    """


class BackupArchiveStore(abc.ABC):
    """Port: the backup layer's seam to the authoritative-copy archive store."""

    @abc.abstractmethod
    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        """Archive the authoritative ``current/`` under the given ref (FR-BAK-1).

        The caller pre-generates the ref and commits the metadata row first (#1707),
        then writes the archive. A crash after the row commit but before the archive
        write leaves a dangling row (detectable, self-healing), never an orphaned
        archive with no row. Raises :class:`BackupNotFoundError` if nothing is
        published to archive.
        """

    @abc.abstractmethod
    async def list_archive_refs(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> list[str]:
        """List all archive refs on storage (filesystem/object-store driven).

        Returns every ref that has a physical archive, regardless of whether a
        metadata row exists. Used by ``DeleteServer`` to prune orphaned archives
        that have no row (issue #1707).
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
        """Check a stored archive (the sweep probe, issue #744).

        Read-only — ``current`` is never touched. Returns the corrupt region-file
        count (``0`` when healthy) so the sweep persists ``HEALTHY`` /
        ``QUARANTINED`` on the backup row.

        On the fs backend the probe extracts the archive into throwaway staging and
        walks it for corrupt ``.mca`` region files (issue #738). On the object
        backend it instead streams the stored archive end to end to prove the store
        can still produce it (issue #2371), returning ``0`` when it can and raising
        :class:`BackupUnreadableError` when it cannot.

        Raises :class:`BackupNotFoundError` for an unknown ref, and
        :class:`BackupStorageUnavailableError` when the backend could not serve the
        read at all — an availability failure, NOT a verdict about the archive, so
        the sweep must not classify the row from it.
        """

    @abc.abstractmethod
    async def check_current_health(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> int | SnapshotScan:
        """Structurally fsck the on-disk authoritative snapshot (the sweep, issue #744).

        Read-only: walks ``current/`` for corrupt ``.mca`` region files (issue #738)
        in place — a published snapshot is immutable/quiesced, so no staging is
        needed and ``current`` is never mutated. Returns the corrupt region-file
        count (``0`` when healthy), or a :class:`SnapshotScan` when there is no such
        count to report: ``NOT_PUBLISHED`` when nothing has been published (the sweep
        skips the server's snapshot without erroring), and ``NOT_EXAMINED`` when the
        backend walks no published snapshot at all (issue #2377), so the sweep can
        report it as unexamined instead of counting a verdict nothing produced.
        """

    @abc.abstractmethod
    async def delete(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        """Remove an archive. Idempotent (a missing archive is a no-op)."""

    @abc.abstractmethod
    async def prune_to_final_snapshot(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> None:
        """Collapse the working set to one retained final-state archive (issue #777).

        The DeleteServer reclaim path: pack the authoritative ``current/`` into a
        single retained ``tar.gz`` and drop the unpacked working-set tree, leaving
        ``backups/`` for the caller to prune separately. Packing is mandatory and
        fail-closed — a pack failure leaves the working set intact and the error
        propagates, so a failed delete never silently loses the latest state. A
        server with no published snapshot is a no-op (nothing to pack).
        """

    @abc.abstractmethod
    def open(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        storage_ref: str,
        byte_range: tuple[int, int] | None = None,
    ) -> AsyncIterator[bytes]:
        """Open a read stream over an archive in its native format (issue #281).

        Streams the stored bytes verbatim (no recompression) for download. Raises
        :class:`BackupNotFoundError` for an unknown ref.

        ``byte_range`` is an INCLUSIVE ``(first, last)`` byte-position pair,
        already resolved against :meth:`size`: the stream then yields exactly
        ``last - first + 1`` bytes, which is what a download resumed with
        ``Range`` needs (issue #2372). The storage side reads only those bytes —
        the tail of a multi-GB archive never pulls the head.
        """

    @abc.abstractmethod
    async def store(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        stream: AsyncIterator[bytes],
        storage_ref: str,
    ) -> None:
        """Store an uploaded archive verbatim under the given ref (issue #281).

        The caller pre-generates the ref and commits the metadata row first (#1707),
        then stores the archive bytes. A crash after the row commit but before the
        store write leaves a dangling row (detectable, self-healing), never an
        orphaned archive with no row.
        """

    @abc.abstractmethod
    async def size(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> int:
        """Return an archive's size in bytes (issue #281).

        Raises :class:`BackupNotFoundError` for an unknown ref, and
        :class:`BackupStorageUnavailableError` when the backend could not answer at
        all (issue #2378). Both the download route's declared ``Content-Length`` and
        the lazy size backfill behind the backup listing/statistics rest on that
        distinction: a missing archive is a fact about one row, an unavailable store
        is a transient condition the caller reports as 503 (issue #2405).
        """
