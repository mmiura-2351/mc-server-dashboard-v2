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
from mc_server_dashboard_api.storage.integrity.region import WorkingSetReport

# A byte stream: async so an adapter can read/transfer without buffering the whole
# working set or JAR in memory (STORAGE.md Sections 3.1, 3.2).
ByteStream = AsyncIterator[bytes]

# The publisher id recorded for an API-initiated restore (issue #873). A restore is
# an authoritative publish with no producing Worker, so it bumps the generation like
# a snapshot commit and stamps this sentinel as the publisher. Recording a sentinel
# (rather than ``None``) makes the publish-time guard (Section 8) treat an in-flight
# stale snapshot from a real Worker as a different-publisher publish and REFUSE it,
# closing the restore-clobber window (#873): no live Worker can ever legitimately
# claim this id, so the guard never wrongly refuses a same-Worker self-heal.
RESTORE_PUBLISHER = "api-restore"

# The publisher id recorded for an API-initiated authoritative file edit (issue
# #889): ``write_file`` / ``delete_file`` / ``delete_dir`` / ``make_dir`` /
# ``rollback_file`` mutate ``current/`` in place on a stopped server. Like a restore
# (#873) such an edit is an authoritative publish with no producing Worker, so it
# bumps the generation and stamps this sentinel: otherwise a same-worker scratch with
# held == store would skip the post-edit hydrate (#767) and boot the PRE-edit world,
# and an in-flight stale snapshot from that scratch worker would pass the publish-time
# guard (same publisher, base == current) and clobber the edits. Stamping the sentinel
# makes ``base < current`` published by a DIFFERENT publisher, so the guard REFUSES the
# stale snapshot. A distinct sentinel from RESTORE_PUBLISHER (the guard treats them
# identically — both are non-Worker ids) keeps ``current_publisher`` honest for
# debugging: an operator can tell an edit-bumped generation from a restore-bumped one.
# No live Worker can legitimately claim this id, so the guard never wrongly refuses a
# same-Worker self-heal.
API_EDIT_PUBLISHER = "api-edit"


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
    async def commit_snapshot(
        self,
        handle: SnapshotHandle,
        *,
        publisher: str | None = None,
        expected_base: int | None = None,
    ) -> int:
        """Atomically publish the staged snapshot, returning the new generation.

        Atomic publish (STORAGE.md Section 4): move staging into a fresh
        ``snapshots/<id>/``, flip the ``current`` symlink atomically, fsync the
        parent, then reclaim the superseded snapshot. After return, ``current/``
        reflects the complete transfer or the prior copy — never a partial. Refuses
        an un-signalled-complete transfer with
        :class:`~.errors.IncompleteTransferError`.

        Bumps and returns the per-server working-set GENERATION (issue #763): a
        monotonically increasing counter the adapter persists alongside the server's
        snapshots, incremented on each successful publish. The caller hands it to
        the Worker over the data plane as the ``X-Working-Set-Generation`` response
        header, so the reconciler can hydrate only when a Worker holds a STALE
        generation (presence at a fresh-enough generation, generalizing the
        presence-only skip of issue #698). The reconciler reads the authoritative
        generation directly from Storage (``Storage.current_generation``), not from
        a DB column. The bump is part of the publish, so the
        persisted generation and ``current/`` never disagree. Refused publishes
        (integrity gate, incomplete transfer) do NOT bump.

        ``publisher`` records the id of the Worker producing this snapshot alongside
        the generation (issue #847 bug 3), read back via :meth:`current_publisher`.
        The publish-time generation guard uses it to allow a same-Worker re-publish
        whose base lags the store by a lost response (self-heal) while still refusing
        a DIFFERENT Worker's stale-scratch publish (A->B->A). ``None`` (a Worker that
        does not declare its id, or an older Worker) records no publisher, so the
        guard cannot prove a foreign publisher and stays permissive — the structural
        #847 fix already prevents the genuine stale cross-worker publish.

        ``expected_base`` closes the upload-window clobber the pre-stream guard
        cannot (issue #899). The data-plane publish guard evaluates the
        base-generation claim ONCE, before the (multi-minute) upload stream; an
        at-rest edit or a backup restore can advance the store AFTER the guard
        passed. ``expected_base`` is the authoritative generation the guard observed
        (what ``current`` was at guard time); the commit re-reads the generation
        under the same per-server serialization the bump uses and raises
        :class:`~.errors.StaleGenerationError` when it advanced past
        ``expected_base`` — the staging is discarded and ``current`` keeps the newer
        copy (no bump), so the Worker re-bases on its next start. ``None`` (a Worker
        that never hydrated, or an older Worker that sends no base) skips the
        re-check, matching the pre-stream guard's backward-compatible posture.

        The content-integrity gate uses the single region rule set (issue #927): a
        non-4096-aligned tail is the normal on-disk shape of a 26.x world, not a tear,
        on every source — running OR stopped. The earlier source-keyed strict/live
        split (issue #923) relied on a ``stopped => 4096-padded`` invariant that does
        not survive a sweep-stop timeout, SIGKILL, OOM, or crash, so strict would
        refuse the stop-leg checkpoint exactly when it is the last chance to capture
        the world. The byte-precise check still catches realistic tears (a referenced
        chunk overrunning EOF, an entry past EOF, a severed prefix). See the STORAGE.md
        Section 8 region-rule note.
        """

    @abc.abstractmethod
    async def abort_snapshot(self, handle: SnapshotHandle) -> None:
        """Discard an incomplete/failed transfer (delete the staging area).

        ``current/`` is untouched. Idempotent: a second abort, or an abort of an
        already-swept handle, is a no-op (the crash-recovery cleanup path,
        Section 4.3).
        """

    @abc.abstractmethod
    async def current_generation(
        self, community_id: CommunityId, server_id: ServerId
    ) -> int:
        """Return the current authoritative working-set generation (issue #763).

        The counter :meth:`commit_snapshot` bumps, read back so the hydrate data
        plane can stamp the generation it serves onto the transfer. A server with no
        published snapshot (never committed) is generation 0, the same value the
        Worker records for an empty/never-hydrated working set, so the reconciler's
        ``worker-gen < store-gen`` comparison treats "nothing published" and "nothing
        held" consistently.
        """

    @abc.abstractmethod
    async def current_publisher(
        self, community_id: CommunityId, server_id: ServerId
    ) -> str | None:
        """Return the id of the Worker that published ``current`` (issue #847 bug 3).

        The value :meth:`commit_snapshot` recorded for the latest successful publish,
        read back by the publish-time generation guard so it can distinguish a
        same-Worker re-publish (lost-response self-heal) from a different-Worker
        stale-scratch publish (A->B->A). ``None`` when nothing is published, or when
        the last publish recorded no publisher (an older Worker) — in which case the
        guard cannot prove a foreign publisher and stays permissive.
        """

    @abc.abstractmethod
    async def check_current_health(
        self, community_id: CommunityId, server_id: ServerId
    ) -> WorkingSetReport:
        """Structurally fsck the on-disk authoritative snapshot (issue #744).

        The one-shot sweep's per-snapshot probe: walk ``current/`` for corrupt
        ``.mca`` region files (issue #738). A published snapshot is immutable and
        quiesced, so the scan is safe in place and needs no staging. Read-only — it
        never mutates ``current``. Raises :class:`~.errors.NotFoundError` if no
        snapshot has been published.
        """

    @abc.abstractmethod
    async def prune_to_final_snapshot(
        self, community_id: CommunityId, server_id: ServerId
    ) -> None:
        """Collapse a server's working set to one retained final-state archive (#777).

        The DeleteServer reclaim path: pack the current authoritative working set
        (``current/``) into a single self-contained ``tar.gz`` retained at the
        server root, then remove everything else under the server prefix that is not
        a backup archive — the unpacked working-set tree (``snapshots/``,
        ``incoming/``, ``versions/``), the ``current`` pointer, and the generation
        marker. ``backups/`` is left untouched — the caller prunes archives (keep
        newest, delete the rest) through its own seam. After return, the server's
        only non-backup artifact is the final tar.gz, which carries no DB row.

        Packing is mandatory and fail-closed: if the pack fails the working-set tree
        is left intact and the error propagates, so a failed delete never silently
        loses the latest state. A server with no published snapshot has nothing to
        pack and is a no-op (idempotent). The retained tar.gz uses the same codec as
        a backup archive (Section 2), so an operator can re-import it.

        Crash-retry safety: the pointer (object) / ``current`` symlink (fs) — the one
        marker that says the working set is still live and re-packable — is
        invalidated the instant the final tar.gz is durable, before any other GC. A
        retried delete that finds no pointer treats the working-set prune as already
        done: it finishes the GC but never re-packs, so it cannot overwrite the good
        final tar.gz with an empty/partial pack from a half-deleted source (#777).

        Unlike :meth:`BackupStore.create_backup_from_current`, the final pack does
        NOT gate on the #764 ``.mca`` integrity check: a corrupt server must still be
        deletable, so a structurally torn region is packed as-is rather than blocking
        the reclaim.
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
        self,
        community_id: CommunityId,
        server_id: ServerId,
        key: BackupKey | None = None,
    ) -> BackupKey:
        """Archive the authoritative ``current/`` into ``backups/`` (FR-BAK-1).

        The stopped-server path: Storage only ever archives the authoritative copy
        (Section 3.3). When *key* is provided the archive is stored under that key
        (the caller pre-generated it so it could commit the metadata row first,
        issue #1707); when ``None`` a fresh key is minted internally.
        Raises :class:`~.errors.NotFoundError` if nothing is published.
        """

    @abc.abstractmethod
    async def list_backups(
        self, community_id: CommunityId, server_id: ServerId
    ) -> list[BackupKey]:
        """Enumerate a server's backup keys (metadata lives in the DB, #15)."""

    @abc.abstractmethod
    async def restore_backup(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        key: BackupKey,
        *,
        force: bool = False,
    ) -> WorkingSetReport:
        """Atomically republish a backup into ``current/`` (FR-BAK-4, issue #743).

        Atomic publish (Section 4): stage the extracted archive, run the
        restore-direction integrity gate over it (issue #738), then flip. A backup
        predating the create gate (#749) or an uploaded one may carry structurally
        corrupt ``.mca`` region files; by default (``force=False``) a corrupt
        staging is refused with :class:`~.errors.IntegrityCheckError` (carrying the
        report) and ``current`` is left untouched (last-known-good, #703). With
        ``force=True`` the operator override publishes the corrupt working set
        anyway (better a deliberate corrupt restore than none, #703).

        Returns the :class:`~...integrity.region.WorkingSetReport` of the extracted
        working set either way (healthy on a clean restore, non-healthy on a forced
        corrupt one) so the caller can quarantine + audit. The application enforces
        the stop precondition; Storage enforces atomicity and stays DB-free. Raises
        :class:`~.errors.NotFoundError` for an unknown key.
        """

    @abc.abstractmethod
    async def check_backup_health(
        self, community_id: CommunityId, server_id: ServerId, key: BackupKey
    ) -> WorkingSetReport:
        """Extract a backup archive and structurally fsck it (issue #744).

        The one-shot sweep's per-backup probe: extract the archive into throwaway
        staging under the decompressed-byte cap (the restore extractor), walk it for
        corrupt ``.mca`` region files (issue #738), then discard the staging.
        Read-only — it never publishes and never touches ``current`` — so the caller
        persists the verdict (HEALTHY/QUARANTINED) in the DB. Re-running yields the
        same report. Raises :class:`~.errors.NotFoundError` for an unknown key.
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
        self,
        community_id: CommunityId,
        server_id: ServerId,
        stream: ByteStream,
        key: BackupKey | None = None,
    ) -> BackupKey:
        """Store an uploaded backup archive verbatim, returning its key (issue #281).

        The caller (the upload use case) has already VALIDATED the archive opens and
        its entries are traversal-safe; Storage stores the bytes under the provided
        *key* (when the caller pre-generated it, issue #1707) or a fresh
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

        **Crash-atomicity (issue #1608):** the deletion is not crash-atomic
        across the subtree. fs uses ``shutil.rmtree`` (per-member unlinks);
        object uses a per-object delete loop. A crash mid-op leaves a
        partially deleted subtree at a stale generation. Recovery: re-run the
        same delete (converges) or restore a backup (STORAGE.md Section 3.3).
        """

    @abc.abstractmethod
    async def rename_file(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        from_path: RelPath,
        to_path: RelPath,
    ) -> None:
        """Rename/move a single file within ``current/`` (issue #1164).

        No version capture on either side: this is a pure rename with no content
        change, so retaining versions would waste storage (the bytes are
        identical). The caller's content-addressed cache (plugin JARs) or backups
        cover recovery. Raises :class:`~.errors.NotFoundError` for a missing
        source file.

        Atomic on fs (``rename(2)``); on object backends it is a copy+delete
        pair — not crash-atomic (issue #1608).
        """

    @abc.abstractmethod
    async def rename_dir(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        from_path: RelPath,
        to_path: RelPath,
    ) -> None:
        """Rename/move a directory within ``current/`` (issue #1191).

        Like :meth:`delete_dir`, no per-file version capture: whole-subtree
        recovery is the backups' job (Section 3.3). Raises
        :class:`~.errors.NotFoundError` for a missing source directory.

        Atomic on fs (``rename(2)``); on object backends it is a per-object
        copy+delete loop — not crash-atomic (issue #1608).
        """

    @abc.abstractmethod
    async def make_dir(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        """Create an (empty) directory in ``current/`` (issue #259).

        fs / remote-fs materialize a real empty directory. **Object storage has no
        real directories** — a directory exists only as the shared key-prefix of
        its files (Section 7.3), so an *empty* directory has no key to make it
        visible; ``make_dir`` writes a zero-byte ``.dir`` marker object under the
        prefix so the directory shows up in listings (issue #1125). ``list_dir``
        filters the marker out of its entries, but the marker is a real object: it
        rides the hydrate tar to the Worker (a literal ``foo/.dir`` file appears in
        the live working directory), is re-packed into the next snapshot, and is
        carried into backups/restores. Idempotent: creating an existing directory is
        fine. See STORAGE.md Section 3.4 for the full lifecycle note.
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
