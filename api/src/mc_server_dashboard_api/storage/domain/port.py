"""The ``Storage`` Port (docs/app/STORAGE.md Section 3).

The API-side authoritative store for world data, server JARs, and backups
(FR-DATA-1). It is the only component that touches the pluggable backend; callers
depend on this Port and the wiring binds a config-selected adapter (fs / remote-fs
/ object) with no change to any use case (FR-DATA-2).

The contract is split into five cohesive Port interfaces along the requirement
areas of Section 3 — working-set hydrate/snapshot, JAR store/reuse, backup
archives, authoritative-copy file edits, and file version retention — and
:class:`Storage` composes all five. A use case may depend on the narrow slice it
needs; the wiring binds one adapter that satisfies the whole contract.

Every operation is scoped by an explicit ``(community_id, server_id)`` (JARs are
unscoped), so the adapter resolves the namespace and enforces isolation; no
operation accepts an absolute path. Reads expose byte streams (``AsyncIterator``)
and writes accept them, so the data plane (epic #8) streams through without full
buffering. The wire transport across the API<->Worker boundary is *not* part of
this Port (Section 3.1, decision 8.3); Storage only guarantees the
authoritative-side semantics.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass

from mc_server_dashboard_api.storage.domain.value_objects import (
    BackupKey,
    CommunityId,
    JarKey,
    RelPath,
    ServerId,
    VersionId,
)

# A byte stream: async so an adapter can read/transfer without buffering the whole
# working set or JAR in memory (STORAGE.md Sections 3.1, 3.2).
ByteStream = AsyncIterator[bytes]


@dataclass(frozen=True)
class DirEntry:
    """One entry in a ``current/`` directory listing (STORAGE.md Section 3.4)."""

    name: str
    is_dir: bool
    size: int


class SnapshotHandle(abc.ABC):
    """An opaque handle to an in-flight incoming snapshot transfer.

    Returned by :meth:`WorkingSetStore.begin_snapshot`; identifies the isolated
    ``incoming/<transfer-id>/`` staging area the bytes land in until publish. The
    adapter owns its representation; callers only pass it back to ``write_snapshot``
    / ``commit_snapshot`` / ``abort_snapshot``.
    """


class WorkingSetStore(abc.ABC):
    """Port slice: working-set hydrate egress + snapshot ingest.

    See STORAGE.md Section 3.1.
    """

    @abc.abstractmethod
    def open_hydrate_source(
        self, community_id: CommunityId, server_id: ServerId
    ) -> ByteStream:
        """Open a read stream over the current authoritative working set.

        The data plane reads from this to feed a Worker on start/relocation
        (hydrate). Reads ``current/``. Raises :class:`~.errors.NotFoundError` if no
        snapshot has been published for the server.

        POSIX semantics reliance: on fs/remote-fs the stream reads the snapshot
        directory ``current`` resolved to *at open time*. A concurrent publish flips
        ``current`` to a new snapshot and only then reclaims the superseded one, so a
        hydrate already reading the old snapshot keeps reading complete bytes (the
        directory it holds open is not unlinked out from under it).
        """

    @abc.abstractmethod
    async def begin_snapshot(
        self, community_id: CommunityId, server_id: ServerId
    ) -> SnapshotHandle:
        """Start an incoming snapshot transfer (allocate ``incoming/<id>/`` staging)."""

    @abc.abstractmethod
    async def write_snapshot(self, handle: SnapshotHandle, stream: ByteStream) -> None:
        """Stream the Worker's working set into staging only — never ``current/``.

        May be called incrementally; each chunk lands under the handle's staging
        area. ``current/`` is untouched until :meth:`commit_snapshot`.
        """

    @abc.abstractmethod
    async def commit_snapshot(self, handle: SnapshotHandle) -> None:
        """Atomically publish the staged snapshot as the new authoritative copy.

        Atomic publish (STORAGE.md Section 4): move staging into a fresh
        ``snapshots/<id>/``, flip the ``current`` symlink atomically, fsync the
        parent, then reclaim the superseded snapshot. After return, ``current/``
        reflects the complete transfer or the prior copy — never a partial. Refuses
        an un-signalled-complete transfer with
        :class:`~.errors.IncompleteTransferError`.
        """

    @abc.abstractmethod
    async def abort_snapshot(self, handle: SnapshotHandle) -> None:
        """Discard an incomplete/failed transfer (delete the staging area).

        ``current/`` is untouched. Idempotent: a second abort, or an abort of an
        already-swept handle, is a no-op (the crash-recovery cleanup path,
        Section 4.3).
        """


class JarStore(abc.ABC):
    """Port slice: content-addressed JAR store/reuse (STORAGE.md Section 3.2)."""

    @abc.abstractmethod
    async def put_jar(self, stream: ByteStream) -> JarKey:
        """Store a JAR, returning its content key (its SHA-256).

        Idempotent: storing identical bytes yields the same key and no duplicate.
        """

    @abc.abstractmethod
    async def has_jar(self, key: JarKey) -> bool:
        """Test presence so a fetch from an external source can be skipped."""

    @abc.abstractmethod
    def open_jar(self, key: JarKey) -> ByteStream:
        """Read a stored JAR. Raises :class:`~.errors.NotFoundError` if absent."""


class BackupStore(abc.ABC):
    """Port slice: backup archive create/list/restore/delete.

    See STORAGE.md Section 3.3.
    """

    @abc.abstractmethod
    async def create_backup_from_current(
        self, community_id: CommunityId, server_id: ServerId
    ) -> BackupKey:
        """Archive the authoritative ``current/`` into ``backups/`` (FR-BAK-1).

        The stopped-server path: Storage only ever archives the authoritative copy
        (Section 3.3). Raises :class:`~.errors.NotFoundError` if nothing is published.
        """

    @abc.abstractmethod
    async def list_backups(
        self, community_id: CommunityId, server_id: ServerId
    ) -> list[BackupKey]:
        """Enumerate a server's backup keys (metadata lives in the DB, #15)."""

    @abc.abstractmethod
    async def restore_backup(
        self, community_id: CommunityId, server_id: ServerId, key: BackupKey
    ) -> None:
        """Atomically republish a backup into ``current/`` (FR-BAK-4).

        Atomic publish (Section 4): stage the extracted archive, then flip. The
        application enforces the stop precondition; Storage enforces atomicity.
        Raises :class:`~.errors.NotFoundError` for an unknown key.
        """

    @abc.abstractmethod
    async def delete_backup(
        self, community_id: CommunityId, server_id: ServerId, key: BackupKey
    ) -> None:
        """Remove a backup archive. Idempotent."""


class FileStore(abc.ABC):
    """Port slice: authoritative-copy file read/edit for stopped servers.

    See Section 3.4.
    """

    @abc.abstractmethod
    async def read_file(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> bytes:
        """Read one file from ``current/``. Raises :class:`~.errors.NotFoundError`."""

    @abc.abstractmethod
    async def list_dir(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> list[DirEntry]:
        """Browse a directory in ``current/``. Raises :class:`~.errors.NotFoundError`.

        Pass ``RelPath(".")`` to list the working-set root itself.
        """

    @abc.abstractmethod
    async def write_file(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: RelPath,
        data: bytes,
    ) -> None:
        """Edit one file in ``current/``, retaining the prior version first.

        Captures the previous content into ``versions/`` (Section 5) *before*
        overwriting, then writes atomically (temp-sibling + fsync + rename,
        Section 4.4) so a concurrent read never sees a torn file.
        """


class FileVersionStore(abc.ABC):
    """Port slice: file version retention / rollback (STORAGE.md Section 3.5)."""

    @abc.abstractmethod
    async def list_file_versions(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> list[VersionId]:
        """List retained prior versions of a file, newest-first."""

    @abc.abstractmethod
    async def read_file_version(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: RelPath,
        version_id: VersionId,
    ) -> bytes:
        """Read a specific retained version (preview/diff before rollback)."""

    @abc.abstractmethod
    async def rollback_file(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: RelPath,
        version_id: VersionId,
    ) -> None:
        """Restore a file to a retained version.

        Implemented as a :meth:`FileStore.write_file` of the old content, so the
        pre-rollback content is itself retained (rollback is reversible).
        """


class Storage(
    WorkingSetStore,
    JarStore,
    BackupStore,
    FileStore,
    FileVersionStore,
    abc.ABC,
):
    """The full ``Storage`` Port: the composition of all five slices (Section 3).

    A concrete adapter (``FsStorage``, and later ``ObjectStorage``) implements
    every operation; a use case may depend on one slice. The crash-recovery sweep
    (Section 4.3) is an adapter operation, not part of the abstract contract — it
    is keyed off the live ``current`` target and exposed on the concrete adapter
    for the startup lifespan hook and manual invocation.
    """
