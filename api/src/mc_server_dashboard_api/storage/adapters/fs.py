"""The local-filesystem ``Storage`` adapter (``FsStorage``), STORAGE.md Section 7.1.

Realizes the full :class:`~...domain.port.Storage` Port over a directory tree
rooted at ``<root>`` (Section 2), with the Section 4 atomic-publish mechanics:
stage into ``incoming/`` -> move into a fresh ``snapshots/<id>/`` -> atomic
``current`` symlink flip (same-directory ``os.replace`` of one symlink onto
another) -> parent-dir fsync -> reclaim the superseded snapshot. Single-file
writes (Section 4.4) use temp-sibling + fsync + atomic rename, capturing the prior
version first (Section 5). Path-traversal containment (Section 6) is enforced here
because every backend must get it and a future backend cannot forget it.

The same code serves the ``remote-fs`` family (Section 7.2) when ``<root>`` is a
POSIX mount honouring symlinks + atomic same-dir rename + close-to-open
consistency; no separate code path is required.

Wire format: the hydrate/snapshot byte stream is a **tar stream** of the working
set (stdlib :mod:`tarfile`); the data-plane transport (epic #8) carries it
verbatim. Backups are self-contained ``tar.gz`` archives; the archive codec is
adapter-internal (STORAGE.md Section 2): gzip in M1, with zstd deferred.

Blocking filesystem/tar work runs in a worker thread via
:func:`asyncio.to_thread` so the async Port methods do not stall the event loop.
The hydrate stream is generated incrementally on a producer thread; a failure
there is re-raised to the async consumer so a truncated transfer ends with an
error rather than a silent EOF.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import os
import shutil
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from mc_server_dashboard_api.storage.adapters.failure_seam import (
    FailureSeam,
    PublishPhase,
)
from mc_server_dashboard_api.storage.domain.errors import (
    ArchiveTooLargeError,
    IncompleteTransferError,
    NotFoundError,
    PathTraversalError,
    SnapshotHandleError,
)
from mc_server_dashboard_api.storage.domain.port import (
    ByteStream,
    DirEntry,
    JarPoolEntry,
    JarPoolStats,
    SnapshotHandle,
    Storage,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    BackupKey,
    CommunityId,
    JarKey,
    RelPath,
    ServerId,
    SnapshotId,
    VersionId,
)

# Read/stream chunk size for hydrate / JAR egress.
_CHUNK = 1024 * 1024
_DEFAULT_VERSION_RETENTION = 10

# Decompressed-size cap for restore extraction. The compressed archive body is
# bounded on the way in, but a gzip member can expand ~1000x; the cumulative
# DECOMPRESSED bytes are counted as members are extracted so a bomb cannot fill the
# disk (#287). 8 GiB bounds the amplification while covering a real Minecraft
# world; a constant is intentional (no config knob requested).
_DEFAULT_MAX_RESTORE_BYTES = 8 * 1024 * 1024 * 1024


class _FsSnapshotHandle(SnapshotHandle):
    """Names one ``incoming/<transfer-id>/`` staging area for an in-flight snapshot."""

    def __init__(
        self, community_id: CommunityId, server_id: ServerId, transfer_id: str
    ):
        self.community_id = community_id
        self.server_id = server_id
        self.transfer_id = transfer_id
        # Set true on commit/abort so a reused handle is rejected (protocol safety).
        self.consumed = False


class FsStorage(Storage):
    """Filesystem-backed :class:`Storage`.

    ``version_retention`` bounds per-file retained versions (Section 5; the config
    key is owned by CONFIGURATION.md #16). ``failure_seam`` is the crash-injection
    hook for tests (Section 4.3); production uses the no-op default.
    ``tar_member_hook`` is a test-only seam invoked just before each working-set
    member is added to the hydrate tar, so a test can inject a deterministic
    producer-thread failure and prove it surfaces to the consumer; production
    leaves it ``None``.
    """

    def __init__(
        self,
        root: Path,
        *,
        version_retention: int = _DEFAULT_VERSION_RETENTION,
        max_restore_bytes: int = _DEFAULT_MAX_RESTORE_BYTES,
        failure_seam: FailureSeam | None = None,
        tar_member_hook: Callable[[Path], None] | None = None,
    ) -> None:
        self._root = root
        self._version_retention = version_retention
        self._max_restore_bytes = max_restore_bytes
        self._seam = failure_seam or FailureSeam()
        self._tar_member_hook = tar_member_hook
        # Active-reader leases: a snapshot directory an open hydrate stream is
        # reading is held here (refcounted) so a concurrent publish/sweep does not
        # reclaim it out from under the reader. Guarded by ``_lease_lock`` because
        # leases are taken/released across worker threads (Section 4.2 reader
        # safety). Keyed by the resolved snapshot path.
        self._leases: dict[Path, int] = {}
        # Active-staging handles: the ``incoming/<transfer>/`` dir of each in-flight
        # transfer is held here for the life of its handle (begin -> commit/abort)
        # so a concurrently scheduled sweep does not reclaim its staging dir out
        # from under the active stream (issue #183). fs staging also has an on-disk
        # dir, but the dir alone cannot tell an in-flight transfer from a crash
        # leftover; this in-process set is the pin. A crash leftover has no
        # in-process handle by definition, so a fresh process's sweep still reclaims
        # it. Guarded by ``_lease_lock`` alongside the reader leases.
        self._active_staging: set[Path] = set()
        self._lease_lock = threading.Lock()

    # --- layout helpers ----------------------------------------------------

    def _server_root(self, community_id: CommunityId, server_id: ServerId) -> Path:
        return (
            self._root
            / "communities"
            / str(community_id.value)
            / "servers"
            / str(server_id.value)
        )

    def _current_link(self, community_id: CommunityId, server_id: ServerId) -> Path:
        return self._server_root(community_id, server_id) / "current"

    def _current_dir(self, community_id: CommunityId, server_id: ServerId) -> Path:
        """Resolve the live snapshot directory, or raise NotFoundError if unpublished.

        Reads the ``current`` symlink and joins its target onto ``snapshots/``. The
        target is a bare snapshot name, never a path, so it cannot escape.
        """

        link = self._current_link(community_id, server_id)
        if not link.is_symlink():
            raise NotFoundError(f"no published snapshot for server {server_id.value}")
        target = os.readlink(link)
        snapshot = self._server_root(community_id, server_id) / target
        if not snapshot.is_dir():
            raise NotFoundError(
                f"current snapshot missing for server {server_id.value}"
            )
        return snapshot

    def _jars_dir(self) -> Path:
        return self._root / "jars"

    def _jar_path(self, key: JarKey) -> Path:
        return self._jars_dir() / f"{key.sha256}.jar"

    # --- active-reader leases (Section 4.2 reader safety) -------------------

    def _acquire_lease(self, snapshot: Path) -> None:
        with self._lease_lock:
            self._leases[snapshot] = self._leases.get(snapshot, 0) + 1

    def _release_lease(self, snapshot: Path) -> None:
        with self._lease_lock:
            remaining = self._leases.get(snapshot, 0) - 1
            if remaining > 0:
                self._leases[snapshot] = remaining
            else:
                self._leases.pop(snapshot, None)

    def _is_leased(self, snapshot: Path) -> bool:
        with self._lease_lock:
            return self._leases.get(snapshot, 0) > 0

    # --- active-staging leases (issue #183) --------------------------------

    def _register_staging(self, staging: Path) -> None:
        with self._lease_lock:
            self._active_staging.add(staging)

    def _release_staging(self, staging: Path) -> None:
        with self._lease_lock:
            self._active_staging.discard(staging)

    def _is_staging_active(self, staging: Path) -> bool:
        with self._lease_lock:
            return staging in self._active_staging

    # --- path-traversal containment (Section 6) ----------------------------

    def _safe_target(self, base: Path, rel_path: RelPath) -> Path:
        """Join ``rel_path`` under ``base`` and verify the result stays inside it.

        ``RelPath`` already rejected absolute paths and ``..`` at the string level;
        this catches the filesystem vector — a symlink component that resolves out
        of ``base``. The realpath of the candidate (and of each existing parent) is
        checked against the realpath of ``base``.
        """

        base_real = os.path.realpath(base)
        candidate = base.joinpath(*rel_path.parts)
        resolved = os.path.realpath(candidate)
        if resolved != base_real and not resolved.startswith(base_real + os.sep):
            raise PathTraversalError(
                f"rel_path {rel_path.value!r} escapes the server root"
            )
        return Path(resolved)

    # --- crash-recovery sweep (Section 4.3) --------------------------------

    def sweep(self) -> None:
        """Reclaim orphaned staging dirs and superseded snapshots, idempotently.

        Keyed off the live ``current`` target (Section 4.3): for every server, every
        ``snapshots/<id>/`` not pointed at by ``current`` is unreferenced and is
        removed, and every leftover ``incoming/`` staging dir is removed.
        Safe to re-run; never touches the snapshot ``current`` resolves to. Exposed
        for the API startup lifespan hook and manual invocation.

        In-flight staging (issue #183): a transfer staged but not yet committed is
        pinned by an in-process active-staging lease taken at ``begin_snapshot`` (and
        at ``restore_backup``) and released at commit/abort, so a sweep scheduled
        concurrently with an in-flight stage leaves its staging dir intact. A crash
        leftover has no in-process handle by definition, so a fresh process's sweep
        still reclaims it.
        """

        communities = self._root / "communities"
        if not communities.is_dir():
            return
        for community in communities.iterdir():
            servers = community / "servers"
            if not servers.is_dir():
                continue
            for server in servers.iterdir():
                self._sweep_server(server)

    def _sweep_server(self, server_root: Path) -> None:
        live = self._live_snapshot_name(server_root)
        snapshots = server_root / "snapshots"
        if snapshots.is_dir():
            for snap in snapshots.iterdir():
                # Skip the live snapshot and any superseded one an active hydrate
                # reader still holds a lease on; the next sweep reclaims it once the
                # reader releases (Section 4.2 reader safety).
                if snap.name != live and not self._is_leased(snap):
                    _rmtree(snap)
        incoming = server_root / "incoming"
        if incoming.is_dir():
            for staging in incoming.iterdir():
                # Skip an in-flight transfer's staging dir: it is pinned by an
                # active-staging lease until commit/abort (issue #183). A crash
                # leftover has no in-process handle, so it is not skipped.
                if not self._is_staging_active(staging):
                    _rmtree(staging)

    def _live_snapshot_name(self, server_root: Path) -> str | None:
        link = server_root / "current"
        if not link.is_symlink():
            return None
        # The symlink target is a relative ``snapshots/<id>`` path; the live name is
        # its final component.
        return Path(os.readlink(link)).name

    # --- working-set hydrate / snapshot (Section 3.1) ----------------------

    def open_hydrate_source(
        self, community_id: CommunityId, server_id: ServerId
    ) -> ByteStream:
        # The live snapshot is resolved and leased on the FIRST iteration, not at
        # open time: a caller that opens the stream but never iterates/closes it
        # must not pin a snapshot forever (otherwise reclaim + sweep are starved).
        # Re-resolving on first read also means the leased snapshot is exactly the
        # one whose bytes are streamed (Section 4.2 reader safety).
        def _open() -> tuple[Path, Callable[[], None]]:
            current = self._current_dir(community_id, server_id)
            self._acquire_lease(current)
            return current, lambda: self._release_lease(current)

        return _tar_stream(_open, self._tar_member_hook)

    async def begin_snapshot(
        self, community_id: CommunityId, server_id: ServerId
    ) -> SnapshotHandle:
        transfer_id = uuid.uuid4().hex
        staging = self._staging_dir(community_id, server_id, transfer_id)
        await asyncio.to_thread(staging.mkdir, parents=True, exist_ok=False)
        # Pin the staging dir so a concurrent sweep skips it until commit/abort
        # releases it (issue #183).
        self._register_staging(staging)
        return _FsSnapshotHandle(community_id, server_id, transfer_id)

    def _staging_dir(
        self, community_id: CommunityId, server_id: ServerId, transfer_id: str
    ) -> Path:
        return self._server_root(community_id, server_id) / "incoming" / transfer_id

    async def write_snapshot(self, handle: SnapshotHandle, stream: ByteStream) -> None:
        fs_handle = _as_fs_handle(handle)
        staging = self._staging_dir(
            fs_handle.community_id, fs_handle.server_id, fs_handle.transfer_id
        )
        if not staging.is_dir():
            raise SnapshotHandleError("snapshot staging area is gone")
        # Spool the incoming tar to a temp file in the staging dir (disk, bounded
        # RAM — never the whole working set in memory), then stream-extract it.
        # Extraction is sandboxed to ``staging`` (filter="data" refuses absolute /
        # ``..`` members), so a hostile snapshot cannot escape. A file-backed spool
        # is used rather than an os.pipe->thread bridge: it keeps RAM bounded just
        # the same with far simpler code, and the bytes land on the same disk the
        # extraction targets anyway.
        fd, spool_name = await asyncio.to_thread(
            tempfile.mkstemp, dir=str(staging), prefix=".snapshot.", suffix=".tar"
        )
        spool = Path(spool_name)
        try:
            with os.fdopen(fd, "wb") as out:
                async for chunk in stream:
                    await asyncio.to_thread(out.write, chunk)
            await asyncio.to_thread(_extract_tar_into, spool, staging)
        finally:
            await asyncio.to_thread(spool.unlink, missing_ok=True)

    async def commit_snapshot(self, handle: SnapshotHandle) -> None:
        fs_handle = _as_fs_handle(handle)
        if fs_handle.consumed:
            raise SnapshotHandleError("snapshot handle already committed or aborted")
        staging = self._staging_dir(
            fs_handle.community_id, fs_handle.server_id, fs_handle.transfer_id
        )
        if not staging.is_dir():
            raise IncompleteTransferError("no completed staging area to publish")
        # The "proven complete" gate (STORAGE.md Section 4.1): an empty staging area
        # is not a publishable transfer, so an empty staged dir is refused here too,
        # matching the object adapter. The end-of-stream completeness check (the
        # streamed-byte-count vs. Content-Length match) lives at the data-plane HTTP
        # edge (STORAGE.md Section 8, issue #106): the snapshot endpoint verifies the
        # match and aborts the staging area on a mismatch, so commit is only reached
        # for a transfer proven complete.
        if not await asyncio.to_thread(_dir_has_entries, staging):
            raise IncompleteTransferError("no staged files to publish")
        await asyncio.to_thread(
            self._publish, fs_handle.community_id, fs_handle.server_id, staging
        )
        # Publish moved the staging dir into snapshots/; release its active-staging
        # lease so a later sweep is not blocked by a now-dead handle (issue #183).
        self._release_staging(staging)
        fs_handle.consumed = True

    async def abort_snapshot(self, handle: SnapshotHandle) -> None:
        fs_handle = _as_fs_handle(handle)
        staging = self._staging_dir(
            fs_handle.community_id, fs_handle.server_id, fs_handle.transfer_id
        )
        await asyncio.to_thread(_rmtree, staging)
        self._release_staging(staging)
        fs_handle.consumed = True

    def _publish(
        self, community_id: CommunityId, server_id: ServerId, staging: Path
    ) -> None:
        """The atomic-publish core (Section 4.2), driven on a worker thread.

        Steps, each followed by a failure-seam boundary so a crash at any of them
        leaves ``current`` resolving to one complete snapshot (Section 4.3):
        move staging -> ``snapshots/<id>/``; create a temp symlink; atomically
        replace ``current`` with it; fsync the parent dir; reclaim the old snapshot.
        """

        server_root = self._server_root(community_id, server_id)
        snapshots = server_root / "snapshots"
        snapshots.mkdir(parents=True, exist_ok=True)

        self._seam.reach(PublishPhase.AFTER_STAGE)

        snapshot_id = SnapshotId.new()
        snapshot_dir = snapshots / snapshot_id.value
        # Same-filesystem rename: staging (incoming/) and snapshots/ share <root>
        # (Section 7.1 caveat), so this is an atomic move, never a copy.
        os.replace(staging, snapshot_dir)

        self._seam.reach(PublishPhase.AFTER_MOVE)

        link = server_root / "current"
        old_snapshot_name = self._live_snapshot_name(server_root)
        # New symlink at a temp name in the *same* directory, then atomic rename
        # over ``current`` (Section 4.2). The target is relative so the tree is
        # relocatable.
        tmp_link = server_root / f".current.{uuid.uuid4().hex}"
        os.symlink(os.path.join("snapshots", snapshot_id.value), tmp_link)
        os.replace(tmp_link, link)

        self._seam.reach(PublishPhase.AFTER_FLIP)

        _fsync_dir(server_root)

        self._seam.reach(PublishPhase.AFTER_FSYNC)

        if old_snapshot_name is not None and old_snapshot_name != snapshot_id.value:
            old_snapshot = snapshots / old_snapshot_name
            # An active hydrate reader still streaming the superseded snapshot holds
            # a lease on it; leave it in place (the flip already made the new one
            # authoritative) and let the next sweep/publish reclaim it once the
            # reader releases (Section 4.2 reader safety).
            if not self._is_leased(old_snapshot):
                _rmtree(old_snapshot)

    # --- JAR store / reuse (Section 3.2) -----------------------------------

    async def put_jar(self, stream: ByteStream) -> JarKey:
        jars = self._jars_dir()
        await asyncio.to_thread(jars.mkdir, parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        # Stage to a temp file in the jars dir, hashing as we go, then atomically
        # rename to <sha256>.jar. Identical bytes land on the same name (idempotent).
        fd, tmp_name = await asyncio.to_thread(
            tempfile.mkstemp, dir=str(jars), prefix=".jar.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                async for chunk in stream:
                    hasher.update(chunk)
                    await asyncio.to_thread(out.write, chunk)
                await asyncio.to_thread(out.flush)
                await asyncio.to_thread(os.fsync, out.fileno())
            key = JarKey(hasher.hexdigest())
            await asyncio.to_thread(os.replace, tmp, self._jar_path(key))
        except BaseException:
            await asyncio.to_thread(tmp.unlink, missing_ok=True)
            raise
        return key

    async def has_jar(self, key: JarKey) -> bool:
        return await asyncio.to_thread(self._jar_path(key).is_file)

    def open_jar(self, key: JarKey) -> ByteStream:
        path = self._jar_path(key)
        if not path.is_file():
            raise NotFoundError(f"jar not found: {key.sha256}")
        return _file_stream(path)

    async def jar_pool_stats(self) -> JarPoolStats:
        return await asyncio.to_thread(self._jar_pool_stats)

    def _jar_pool_stats(self) -> JarPoolStats:
        jars = self._jars_dir()
        if not jars.is_dir():
            return JarPoolStats(count=0, total_bytes=0)
        # One directory of content-addressed ``<sha256>.jar`` files; the temp-stage
        # files put_jar leaves on failure are named ``.jar.*.tmp`` and excluded.
        count = 0
        total = 0
        for entry in jars.iterdir():
            if entry.suffix == ".jar" and entry.is_file():
                count += 1
                total += entry.stat().st_size
        return JarPoolStats(count=count, total_bytes=total)

    async def list_jars(self) -> list[JarPoolEntry]:
        return await asyncio.to_thread(self._list_jars)

    def _list_jars(self) -> list[JarPoolEntry]:
        jars = self._jars_dir()
        if not jars.is_dir():
            return []
        # Same content-addressed ``<sha256>.jar`` namespace jar_pool_stats scans;
        # here each entry also carries its size and mtime (the GC safety window,
        # #293). The ``.jar.*.tmp`` stage files are excluded by the ``.jar`` suffix.
        entries: list[JarPoolEntry] = []
        for entry in jars.iterdir():
            if entry.suffix != ".jar" or not entry.is_file():
                continue
            stat = entry.stat()
            entries.append(
                JarPoolEntry(
                    key=JarKey(entry.stem),
                    size_bytes=stat.st_size,
                    modified_at=dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.UTC),
                )
            )
        return entries

    async def delete_jar(self, key: JarKey) -> None:
        await asyncio.to_thread(self._jar_path(key).unlink, missing_ok=True)

    # --- backup archive create / list / restore / delete (Section 3.3) -----

    async def create_backup_from_current(
        self, community_id: CommunityId, server_id: ServerId
    ) -> BackupKey:
        current = await asyncio.to_thread(self._current_dir, community_id, server_id)
        backups = self._server_root(community_id, server_id) / "backups"
        await asyncio.to_thread(backups.mkdir, parents=True, exist_ok=True)
        key = BackupKey(uuid.uuid4().hex)
        archive = backups / f"{key.value}.tar.gz"
        await asyncio.to_thread(_write_tar_gz, current, archive)
        return key

    async def list_backups(
        self, community_id: CommunityId, server_id: ServerId
    ) -> list[BackupKey]:
        backups = self._server_root(community_id, server_id) / "backups"
        if not await asyncio.to_thread(backups.is_dir):
            return []
        names = await asyncio.to_thread(
            lambda: sorted(p.name for p in backups.iterdir())
        )
        return [
            BackupKey(name[: -len(".tar.gz")])
            for name in names
            if name.endswith(".tar.gz")
        ]

    async def restore_backup(
        self, community_id: CommunityId, server_id: ServerId, key: BackupKey
    ) -> None:
        archive = (
            self._server_root(community_id, server_id)
            / "backups"
            / f"{key.value}.tar.gz"
        )
        if not await asyncio.to_thread(archive.is_file):
            raise NotFoundError(f"backup not found: {key.value}")
        # Stage the extracted archive into incoming/restore-<id>/, then publish it
        # through the same atomic path as a snapshot (Section 4.1).
        staging = (
            self._server_root(community_id, server_id)
            / "incoming"
            / f"restore-{key.value}-{uuid.uuid4().hex}"
        )
        await asyncio.to_thread(staging.mkdir, parents=True, exist_ok=False)
        # A restore stages under incoming/ exactly like a snapshot, so pin it with
        # the same active-staging lease for the life of the operation (issue #183).
        self._register_staging(staging)
        try:
            await asyncio.to_thread(
                _extract_tar_gz_into, archive, staging, self._max_restore_bytes
            )
            await asyncio.to_thread(self._publish, community_id, server_id, staging)
        except BaseException:
            await asyncio.to_thread(_rmtree, staging)
            raise
        finally:
            self._release_staging(staging)

    async def delete_backup(
        self, community_id: CommunityId, server_id: ServerId, key: BackupKey
    ) -> None:
        archive = self._backup_path(community_id, server_id, key)
        await asyncio.to_thread(archive.unlink, missing_ok=True)

    def _backup_path(
        self, community_id: CommunityId, server_id: ServerId, key: BackupKey
    ) -> Path:
        return (
            self._server_root(community_id, server_id)
            / "backups"
            / f"{key.value}.tar.gz"
        )

    def open_backup(
        self, community_id: CommunityId, server_id: ServerId, key: BackupKey
    ) -> ByteStream:
        # Stream the stored archive bytes verbatim (no recompression): the file is
        # already a self-contained tar.gz (issue #281).
        archive = self._backup_path(community_id, server_id, key)
        if not archive.is_file():
            raise NotFoundError(f"backup not found: {key.value}")
        return _file_stream(archive)

    async def put_backup(
        self, community_id: CommunityId, server_id: ServerId, stream: ByteStream
    ) -> BackupKey:
        # Store the uploaded archive bytes verbatim under a fresh key (the caller
        # already validated the archive). Stage to a temp file in backups/, then
        # atomically rename to <key>.tar.gz so a partial upload never appears as a
        # listable backup (issue #281).
        backups = self._server_root(community_id, server_id) / "backups"
        await asyncio.to_thread(backups.mkdir, parents=True, exist_ok=True)
        key = BackupKey(uuid.uuid4().hex)
        fd, tmp_name = await asyncio.to_thread(
            tempfile.mkstemp, dir=str(backups), prefix=".backup.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                async for chunk in stream:
                    await asyncio.to_thread(out.write, chunk)
                await asyncio.to_thread(out.flush)
                await asyncio.to_thread(os.fsync, out.fileno())
            await asyncio.to_thread(
                os.replace, tmp, self._backup_path(community_id, server_id, key)
            )
        except BaseException:
            await asyncio.to_thread(tmp.unlink, missing_ok=True)
            raise
        return key

    async def backup_size(
        self, community_id: CommunityId, server_id: ServerId, key: BackupKey
    ) -> int:
        archive = self._backup_path(community_id, server_id, key)
        if not await asyncio.to_thread(archive.is_file):
            raise NotFoundError(f"backup not found: {key.value}")
        return await asyncio.to_thread(lambda: archive.stat().st_size)

    # --- file read / edit on the authoritative copy (Section 3.4) ----------

    async def read_file(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> bytes:
        return await asyncio.to_thread(
            self._read_file, community_id, server_id, rel_path
        )

    def _read_file(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> bytes:
        current = self._current_dir(community_id, server_id)
        target = self._safe_target(current, rel_path)
        if not target.is_file():
            raise NotFoundError(f"file not found: {rel_path.value}")
        return target.read_bytes()

    def open_file_stream(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> ByteStream:
        # The per-file analogue of open_hydrate_source (issue #265): the live
        # snapshot is resolved and leased on the FIRST iteration, not at open
        # time, so a stream opened but never consumed never pins a snapshot, and
        # the leased snapshot is exactly the one the file is read out of (Section
        # 4.2 reader safety). The lease protects the snapshot dir from a
        # concurrent publish/sweep for the whole duration of a large read.
        def _open() -> tuple[Path, Callable[[], None]]:
            current = self._current_dir(community_id, server_id)
            self._acquire_lease(current)
            try:
                target = self._safe_target(current, rel_path)
                if not target.is_file():
                    raise NotFoundError(f"file not found: {rel_path.value}")
            except BaseException:
                self._release_lease(current)
                raise
            return target, lambda: self._release_lease(current)

        return _leased_file_stream(_open)

    async def list_dir(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> list[DirEntry]:
        return await asyncio.to_thread(
            self._list_dir, community_id, server_id, rel_path
        )

    def _list_dir(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> list[DirEntry]:
        # A never-snapshotted server has an empty working set, not a missing one
        # (issue #205): list it as empty rather than raising, mirroring the data
        # plane's JAR-only hydrate posture for the unpublished state.
        if not self._current_link(community_id, server_id).is_symlink():
            return []
        current = self._current_dir(community_id, server_id)
        target = self._safe_target(current, rel_path)
        if not target.is_dir():
            raise NotFoundError(f"directory not found: {rel_path.value}")
        entries = []
        for child in sorted(target.iterdir(), key=lambda p: p.name):
            is_dir = child.is_dir()
            entries.append(
                DirEntry(
                    name=child.name,
                    is_dir=is_dir,
                    size=0 if is_dir else child.stat().st_size,
                )
            )
        return entries

    async def write_file(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: RelPath,
        data: bytes,
    ) -> None:
        await asyncio.to_thread(
            self._write_file, community_id, server_id, rel_path, data
        )

    def _write_file(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: RelPath,
        data: bytes,
    ) -> None:
        # The working-set root (an empty rel_path, e.g. the file route's default
        # ``path="."``) names a directory, not a file. Writing it would target the
        # live snapshot directory itself and the atomic rename onto a directory
        # raises IsADirectoryError (the at-rest 500, issue #542); refuse it as an
        # invalid path instead.
        if not rel_path.parts:
            raise PathTraversalError("rel_path must name a file, not the root")
        # A never-snapshotted server has no live snapshot to edit in place (issue
        # #205). Initialize the first published version containing just this file,
        # through the same atomic-publish path a snapshot uses: stage the file into
        # an incoming/ dir, then flip ``current`` onto it. A snapshot publishing
        # concurrently stages into its own incoming/ dir and flips independently —
        # last flip wins, neither corrupts the other (Section 4.2).
        if not self._current_link(community_id, server_id).is_symlink():
            self._publish_initial(community_id, server_id, rel_path, data)
            return
        current = self._current_dir(community_id, server_id)
        target = self._safe_target(current, rel_path)
        # Refuse to overwrite a directory with file bytes (issue #542): the atomic
        # rename onto an existing directory raises IsADirectoryError, so reject it
        # as an invalid path rather than crashing.
        if target.is_dir():
            raise PathTraversalError(
                f"rel_path names a directory, not a file: {rel_path.value}"
            )
        # Capture the prior version BEFORE overwriting (Section 4.4/5), so a crash
        # mid-write leaves both the old content and the retained version consistent.
        if target.is_file():
            self._capture_version(community_id, server_id, rel_path, target)
        self._seam.reach(PublishPhase.AFTER_VERSION_CAPTURE)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, data)

    def _publish_initial(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: RelPath,
        data: bytes,
    ) -> None:
        """Publish the first version of a never-snapshotted server (issue #205).

        Stage just ``rel_path`` into a fresh ``incoming/`` dir, then publish it
        through :meth:`_publish` — the same symlink-flip path a snapshot commit
        uses. The staging dir is pinned with an active-staging lease for the life
        of the operation so a concurrent sweep does not reclaim it (issue #183).
        """

        staging = self._staging_dir(community_id, server_id, uuid.uuid4().hex)
        staging.mkdir(parents=True, exist_ok=False)
        self._register_staging(staging)
        try:
            target = staging.joinpath(*rel_path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(target, data)
            self._publish(community_id, server_id, staging)
        except BaseException:
            _rmtree(staging)
            raise
        finally:
            self._release_staging(staging)

    async def delete_file(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        await asyncio.to_thread(self._delete_file, community_id, server_id, rel_path)

    def _delete_file(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        current = self._current_dir(community_id, server_id)
        target = self._safe_target(current, rel_path)
        if not target.is_file():
            raise NotFoundError(f"file not found: {rel_path.value}")
        # Capture the content BEFORE removing it (Section 5), so a delete is
        # reversible by rollback exactly like an overwrite is.
        self._capture_version(community_id, server_id, rel_path, target)
        self._seam.reach(PublishPhase.AFTER_VERSION_CAPTURE)
        target.unlink()
        _fsync_dir(target.parent)

    async def delete_dir(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        await asyncio.to_thread(self._delete_dir, community_id, server_id, rel_path)

    def _delete_dir(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        current = self._current_dir(community_id, server_id)
        target = self._safe_target(current, rel_path)
        if not target.is_dir():
            raise NotFoundError(f"directory not found: {rel_path.value}")
        # No per-file version capture (Port contract): whole-subtree recovery is
        # the backups' job (Section 3.3), and capturing a version per member would
        # be a storage-amplification bomb on a large subtree.
        shutil.rmtree(target)
        _fsync_dir(target.parent)

    async def make_dir(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        await asyncio.to_thread(self._make_dir, community_id, server_id, rel_path)

    def _make_dir(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        # fs materializes a real empty directory in the live snapshot (it rides the
        # hydrate tar's directory members). A never-snapshotted server has no live
        # snapshot, so _current_dir raises NotFoundError; mkdir requires a working
        # set to exist first (servers seed one at create, issue #243). Idempotent:
        # an existing directory is fine (make_dir contract).
        current = self._current_dir(community_id, server_id)
        target = self._safe_target(current, rel_path)
        target.mkdir(parents=True, exist_ok=True)
        _fsync_dir(target.parent)

    def _atomic_write(self, target: Path, data: bytes) -> None:
        """temp-sibling + fsync + atomic rename (Section 4.4)."""

        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            self._seam.reach(PublishPhase.AFTER_FILE_TEMP_WRITE)
            os.replace(tmp, target)
            _fsync_dir(target.parent)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    # --- file version retention / rollback (Section 3.5, Section 5) ---------

    def _versions_dir(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> Path:
        return self._server_root(community_id, server_id).joinpath(
            "versions", *rel_path.parts
        )

    def _capture_version(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: RelPath,
        source: Path,
    ) -> None:
        """Copy the current content of ``source`` into ``versions/`` and prune."""

        versions = self._versions_dir(community_id, server_id, rel_path)
        versions.mkdir(parents=True, exist_ok=True)
        version_id = _new_version_id()
        shutil.copyfile(source, versions / version_id)
        self._prune_versions(versions)

    async def retain_file_version(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        await asyncio.to_thread(
            self._retain_file_version, community_id, server_id, rel_path
        )

    def _retain_file_version(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        # Retain the current authoritative bytes as a version unless they already
        # equal the newest retained version (issue #351): a running edit snapshots
        # the frozen authoritative copy before each edit, and without this dedup
        # repeated edits to one file would push identical copies into the bounded
        # ring and evict distinct at-rest versions.
        try:
            current = self._current_dir(community_id, server_id)
        except NotFoundError:
            return  # never-published server: nothing authoritative to retain
        target = self._safe_target(current, rel_path)
        if not target.is_file():
            return  # no authoritative copy yet: nothing to retain
        versions = self._versions_dir(community_id, server_id, rel_path)
        if self._matches_newest_version(versions, target):
            return  # unchanged since the newest retained version: skip the churn
        self._capture_version(community_id, server_id, rel_path, target)

    def _matches_newest_version(self, versions: Path, source: Path) -> bool:
        """True if ``source`` equals the newest retained version under ``versions``.

        Compares by size first (a cheap stat reject), then by SHA-256 so two large
        identical blobs are hashed independently rather than both held in memory.
        """

        if not versions.is_dir():
            return False
        names = sorted(p.name for p in versions.iterdir())
        if not names:
            return False
        newest = versions / names[-1]  # ids are time-ordered (_new_version_id)
        if source.stat().st_size != newest.stat().st_size:
            return False
        return _file_sha256(source) == _file_sha256(newest)

    def _prune_versions(self, versions: Path) -> None:
        existing = sorted(p.name for p in versions.iterdir())
        excess = len(existing) - self._version_retention
        for name in existing[:excess] if excess > 0 else []:
            (versions / name).unlink(missing_ok=True)

    async def list_file_versions(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> list[VersionId]:
        versions = self._versions_dir(community_id, server_id, rel_path)
        if not await asyncio.to_thread(versions.is_dir):
            return []
        names = await asyncio.to_thread(
            lambda: sorted(p.name for p in versions.iterdir())
        )
        # Newest-first (Section 3.5); version ids are time-ordered (_new_version_id).
        return [VersionId(name) for name in reversed(names)]

    async def read_file_version(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: RelPath,
        version_id: VersionId,
    ) -> bytes:
        versions = self._versions_dir(community_id, server_id, rel_path)
        path = versions / version_id.value
        if not await asyncio.to_thread(path.is_file):
            raise NotFoundError(f"version not found: {version_id.value}")
        return await asyncio.to_thread(path.read_bytes)

    async def rollback_file(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: RelPath,
        version_id: VersionId,
    ) -> None:
        # Rollback = write_file of the old content, so the pre-rollback content is
        # itself retained and rollback is reversible (Section 3.5).
        old = await self.read_file_version(
            community_id, server_id, rel_path, version_id
        )
        await self.write_file(community_id, server_id, rel_path, old)


# --- module-level filesystem/tar helpers (run on worker threads) -----------


def _as_fs_handle(handle: SnapshotHandle) -> _FsSnapshotHandle:
    if not isinstance(handle, _FsSnapshotHandle):
        raise SnapshotHandleError("handle was not issued by this adapter")
    return handle


def _new_version_id() -> str:
    """A chronologically sortable, collision-resistant version id.

    A zero-padded fixed-width nanosecond timestamp makes lexicographic order equal
    creation order (so :func:`list_file_versions` newest-first and the oldest-first
    pruning in :meth:`_prune_versions` are both correct); a short random suffix
    de-collides ids minted within the same nanosecond. ``uuid1`` was unsafe here:
    its leading ``time_low`` field wraps roughly every 429 s, so its hex was not
    monotonic and sorting could reorder versions across a wrap.
    """

    return f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}"


def _file_sha256(path: Path) -> str:
    """SHA-256 of a file, read in bounded chunks (never the whole file in RAM)."""

    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            hasher.update(chunk)
    return hasher.hexdigest()


def _dir_has_entries(path: Path) -> bool:
    """True if ``path`` contains at least one entry (the empty-commit gate)."""

    return any(path.iterdir())


def _rmtree(path: Path) -> None:
    """Remove a file/dir/symlink if present; idempotent (no error if absent)."""

    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _fsync_dir(path: Path) -> None:
    """fsync a directory so a rename/flip within it survives power loss.

    See Section 4.2.
    """

    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _tar_stream(
    open_source: Callable[[], tuple[Path, Callable[[], None]]],
    member_hook: Callable[[Path], None] | None = None,
) -> AsyncIterator[bytes]:
    """Stream a tar of the hydrate working set (incremental, error-surfacing).

    ``open_source`` is called on the first iteration: it resolves the live
    snapshot directory and takes the active-reader lease, returning the directory
    and the matching lease-release callback. Deferring it to first iteration
    means a stream that is opened but never consumed never pins a snapshot.

    The tar is generated incrementally in stream mode (``w|``) by a worker thread
    writing into one end of an ``os.pipe``; the generator reads bounded ``_CHUNK``
    blocks from the other end, so peak memory is one pipe buffer plus one chunk —
    never the whole (multi-GB) working set.

    A producer-thread failure is captured and **re-raised to the consumer** after
    the writer is joined, so a partial tar surfaces as an error rather than a
    clean (silently truncated) EOF. The lease is released exactly once when the
    stream finishes, is closed early, or raises.
    """

    async def _gen() -> AsyncIterator[bytes]:
        directory, on_close = await asyncio.to_thread(open_source)
        try:
            read_fd, write_fd = os.pipe()
            holder: list[BaseException] = []
            writer = threading.Thread(
                target=_tar_into_fd,
                args=(directory, write_fd, member_hook, holder),
                daemon=True,
            )
            writer.start()
            try:
                while True:
                    chunk = await asyncio.to_thread(os.read, read_fd, _CHUNK)
                    if not chunk:
                        break
                    yield chunk
            finally:
                # Closing the read end unblocks a writer parked on a full pipe (its
                # next write raises BrokenPipeError, which the writer swallows), so
                # join never hangs on early consumer close.
                os.close(read_fd)
                await asyncio.to_thread(writer.join)
            # The writer finished and we drained to EOF: if it failed mid-tar, the
            # EOF we saw was a truncation, so re-raise its error to the consumer.
            if holder:
                raise holder[0]
        finally:
            on_close()

    return _gen()


def _tar_into_fd(
    directory: Path,
    write_fd: int,
    member_hook: Callable[[Path], None] | None,
    holder: list[BaseException],
) -> None:
    """Write a tar of ``directory`` into ``write_fd`` (stream mode), then close it.

    Any failure other than the consumer closing early is recorded in ``holder``
    so the consumer can re-raise it instead of mistaking the closed pipe for a
    clean end of stream.
    """

    try:
        with (
            os.fdopen(write_fd, "wb") as out,
            tarfile.open(fileobj=out, mode="w|") as tar,
        ):
            for child in sorted(directory.iterdir(), key=lambda p: p.name):
                if member_hook is not None:
                    member_hook(child)
                tar.add(child, arcname=child.name)
    except BrokenPipeError:
        # The consumer closed early; nothing more to write.
        pass
    except BaseException as exc:  # noqa: BLE001 - surfaced to the consumer below
        holder.append(exc)


def _file_stream(path: Path) -> AsyncIterator[bytes]:
    """Yield a stored file's bytes in chunks (JAR egress)."""

    async def _gen() -> AsyncIterator[bytes]:
        handle = await asyncio.to_thread(open, path, "rb")
        try:
            while True:
                chunk = await asyncio.to_thread(handle.read, _CHUNK)
                if not chunk:
                    return
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)

    return _gen()


def _leased_file_stream(
    open_source: Callable[[], tuple[Path, Callable[[], None]]],
) -> AsyncIterator[bytes]:
    """Stream one file's bytes in chunks under an active-reader lease (issue #265).

    ``open_source`` is called on the first iteration: it resolves the live
    snapshot, takes the active-reader lease, locates the target file, and returns
    the file path plus the matching lease-release callback. Deferring it to first
    iteration means a stream opened but never consumed never pins a snapshot
    (mirroring :func:`_tar_stream`). The lease is released exactly once when the
    stream finishes, is closed early, or raises.
    """

    async def _gen() -> AsyncIterator[bytes]:
        path, on_close = await asyncio.to_thread(open_source)
        try:
            handle = await asyncio.to_thread(open, path, "rb")
            try:
                while True:
                    chunk = await asyncio.to_thread(handle.read, _CHUNK)
                    if not chunk:
                        return
                    yield chunk
            finally:
                await asyncio.to_thread(handle.close)
        finally:
            on_close()

    return _gen()


def _extract_tar_into(spool: Path, dest: Path) -> None:
    """Stream-extract the tar at ``spool`` into ``dest``, sandboxed.

    Stream mode (``r|*``) reads the spool incrementally so the whole archive is
    never held in memory at once. ``filter="data"`` (Python 3.12+) refuses absolute
    paths, ``..`` escapes, devices and other unsafe members — the tar-side
    traversal defence.
    """

    with open(spool, "rb") as fileobj, tarfile.open(fileobj=fileobj, mode="r|*") as tar:
        tar.extractall(dest, filter="data")


def _write_tar_gz(directory: Path, archive: Path) -> None:
    """Write a self-contained gzip-compressed tar of ``directory`` to ``archive``."""

    with tarfile.open(archive, mode="w:gz") as tar:
        for child in sorted(directory.iterdir(), key=lambda p: p.name):
            tar.add(child, arcname=child.name)


def _extract_tar_gz_into(archive: Path, dest: Path, max_bytes: int) -> None:
    """Extract a restore ``tar.gz`` into ``dest``, traversal-safe and size-bounded.

    ``filter="data"`` (Python 3.12+) refuses absolute paths, ``..`` escapes, devices
    and other unsafe members — the tar-side traversal defence. On top of it, the
    cumulative DECOMPRESSED bytes are counted as each file member is drained and
    bounded by ``max_bytes``: a gzip member can expand ~1000x past the compressed
    body, so the size cap aborts a bomb (:class:`ArchiveTooLargeError`) before it
    fills the disk (#287). The count is over actual bytes read, not the forgeable
    member header.
    """

    total = 0
    with tarfile.open(archive, mode="r:gz") as tar:
        for member in tar:
            total = _extract_member_capped(tar, member, dest, total, max_bytes)


def _extract_member_capped(
    tar: tarfile.TarFile,
    member: tarfile.TarInfo,
    dest: Path,
    total: int,
    max_bytes: int,
) -> int:
    """Extract one member under the data filter, counting drained file bytes.

    A file member's body is drained in bounded chunks and written out; the running
    decompressed total is checked after every chunk so a single high-ratio member
    aborts mid-write rather than being fully materialized first. Writing the body by
    hand bypasses ``extractall``, so the member's sanitized mode/mtime are reapplied
    afterwards. Directory and other safe non-file members carry no body, so they
    extract through the data filter with no contribution to the count. Returns the
    updated running total.
    """

    safe = tarfile.data_filter(member, str(dest))
    if not safe.isfile():
        tar.extract(safe, dest, filter="data")
        return total
    handle = tar.extractfile(safe)
    if handle is None:  # pragma: no cover - a file member always yields a handle
        return total
    target = dest / safe.name
    target.parent.mkdir(parents=True, exist_ok=True)
    with handle, open(target, "wb") as out:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ArchiveTooLargeError(
                    f"restore archive exceeds {max_bytes} decompressed bytes"
                )
            out.write(chunk)
    # Streaming the body by hand drops the member metadata that ``extractall``
    # would have applied, so restore the sanitized mode/mtime ourselves.
    os.chmod(target, safe.mode)
    os.utime(target, (safe.mtime, safe.mtime))
    return total
