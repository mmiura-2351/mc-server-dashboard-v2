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
import datetime as dt
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


@dataclass(frozen=True)
class JarPoolStats:
    """Aggregate stats for the content-addressed JAR pool (issue #286).

    ``count`` is the number of pooled JARs and ``total_bytes`` their combined
    on-store size. Platform-admin operational visibility only — there is no GC
    here (that is the reference-counted design of #32 / D4).
    """

    count: int
    total_bytes: int


@dataclass(frozen=True)
class JarPoolEntry:
    """One pooled JAR's identity, size, and modification time (issue #293).

    The unit the reference-counted GC scans (D4): ``key`` is the content address,
    ``size_bytes`` the on-store size (the freed-bytes accounting), and
    ``modified_at`` the store/upload time the GC's safety window reads (never
    delete a JAR younger than the window). ``modified_at`` is timezone-aware UTC.
    """

    key: JarKey
    size_bytes: int
    modified_at: dt.datetime


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

    @abc.abstractmethod
    async def jar_pool_stats(self) -> JarPoolStats:
        """Count + total bytes of the pooled JARs (issue #286).

        A bounded scan of the one content-addressed JAR namespace (Section 3.2) —
        no per-server scope. Operational visibility for a platform admin; this is
        not a GC and does not reference-count (that is #32 / D4).
        """

    @abc.abstractmethod
    async def list_jars(self) -> list[JarPoolEntry]:
        """Enumerate the pooled JARs with key, size, and modification time (#293).

        A bounded scan of the one content-addressed ``jars/`` namespace, the input
        the reference-counted GC (D4) diffs against the live reference set. Each
        entry's ``modified_at`` feeds the GC safety window. Sibling of
        :meth:`jar_pool_stats`, which only aggregates the same scan.
        """

    @abc.abstractmethod
    async def delete_jar(self, key: JarKey) -> None:
        """Remove a pooled JAR. Idempotent (no error if absent, like delete_backup).

        The reclaim primitive the GC (D4) calls on an unreferenced JAR. Storage
        only deletes the bytes; the reference decision is the GC's.
        """


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

    @abc.abstractmethod
    def open_backup(
        self, community_id: CommunityId, server_id: ServerId, key: BackupKey
    ) -> ByteStream:
        """Open a read stream over a stored backup archive in its native format.

        Streams the archive bytes verbatim (the adapter-internal ``tar.gz`` codec,
        Section 2) with **no** recompression so a download is the exact stored
        bytes; the edge sets the content-type/disposition (issue #281). Raises
        :class:`~.errors.NotFoundError` for an unknown key.
        """

    @abc.abstractmethod
    async def put_backup(
        self, community_id: CommunityId, server_id: ServerId, stream: ByteStream
    ) -> BackupKey:
        """Store an uploaded backup archive verbatim, returning its key (issue #281).

        The caller (the upload use case) has already VALIDATED the archive opens and
        its entries are traversal-safe; Storage only stores the bytes under a fresh
        ``BackupKey`` in the server's ``backups/``, so the new backup is restorable
        through :meth:`restore_backup` exactly like a created one.
        """

    @abc.abstractmethod
    async def backup_size(
        self, community_id: CommunityId, server_id: ServerId, key: BackupKey
    ) -> int:
        """Return a stored backup archive's size in bytes (issue #281).

        The on-disk archive byte count, recorded as ``size_bytes`` at create/upload.
        Raises :class:`~.errors.NotFoundError` for an unknown key.
        """


class FileStore(abc.ABC):
    """Port slice: authoritative-copy file read/edit for stopped servers.

    See Section 3.4.
    """

    @abc.abstractmethod
    async def read_file(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> bytes:
        """Read one file from ``current/``. Raises :class:`~.errors.NotFoundError`.

        Whole-bytes by design: this is the small-edit / preview read (and the
        plain ``GET ?path=`` base64 route, where the bytes *are* the JSON
        payload). A large single-file *download* must use
        :meth:`open_file_stream` instead so it does not buffer the whole file in
        RAM (issue #265).
        """

    @abc.abstractmethod
    def open_file_stream(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> ByteStream:
        """Open a chunked read stream over one file in ``current/`` (issue #265).

        The per-file analogue of :meth:`open_hydrate_source`: a large single-file
        download streams through without buffering the whole file in memory. The
        contract matches the hydrate stream exactly — the live snapshot is
        resolved and the active-reader lease taken on the FIRST iteration (so a
        stream that is opened but never consumed never pins a snapshot), and the
        lease is released exactly once when the stream finishes, is closed early,
        or raises (Section 4.2 reader safety). Raises
        :class:`~.errors.NotFoundError` if the file (or any published snapshot) is
        absent.
        """

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

    @abc.abstractmethod
    async def delete_file(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        """Delete one file from ``current/``, retaining the prior content first.

        Captures the current content into ``versions/`` (Section 5) *before*
        removing the file, so a delete is reversible the same way an edit is
        (rollback restores the captured version). Raises
        :class:`~.errors.NotFoundError` for a missing path so a no-op delete is
        not silently reported as a success.
        """

    @abc.abstractmethod
    async def delete_dir(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        """Recursively delete a directory subtree from ``current/`` (issue #259).

        Unlike :meth:`delete_file`, a directory delete does **not** capture
        per-file versions: file versioning is the fine-grained single-file-edit
        mechanism (Section 5), whereas whole-subtree recovery is what backups
        (Section 3.3) exist for; capturing a version per member of an arbitrarily
        large subtree would be a storage-amplification bomb for no design benefit.
        Raises :class:`~.errors.NotFoundError` for a missing directory.
        """

    @abc.abstractmethod
    async def make_dir(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        """Create an (empty) directory in ``current/`` (issue #259).

        fs / remote-fs materialize a real empty directory. **Object storage has no
        real directories** — a directory exists only as the shared key-prefix of
        its files (Section 7.3), so an *empty* directory cannot be represented and
        ``make_dir`` is a no-op there; the directory becomes observable once a file
        is written under it. This backend-dependent semantics is the honest
        limitation, documented rather than papered over with a marker object that
        would pollute listings. Idempotent: creating an existing directory is fine.
        """


class FileVersionStore(abc.ABC):
    """Port slice: file version retention / rollback (STORAGE.md Section 3.5)."""

    @abc.abstractmethod
    async def list_file_versions(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> list[VersionId]:
        """List retained prior versions of a file, newest-first."""

    @abc.abstractmethod
    async def retain_file_version(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        """Retain the current ``current/`` bytes of a file as a version, deduped.

        Capture the file's current authoritative content into ``versions/``
        (Section 5) the same way :meth:`FileStore.write_file` does on overwrite,
        but **skip the capture when those bytes equal the newest retained
        version** (compared by size, then content hash, so two large identical
        blobs are not held in memory at once). This is the retain-only-if-changed
        primitive a running-server edit's authoritative snapshot uses (issue #351)
        so repeated identical snapshots do not churn the bounded version ring and
        evict genuinely distinct at-rest versions.

        A missing file (no authoritative copy yet) is a no-op (there is nothing to
        retain), mirroring how a running edit of a not-yet-published file proceeds
        unversioned. Unlike :meth:`FileStore.write_file`, this never mutates
        ``current/`` — it only manages the version ring.
        """

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
