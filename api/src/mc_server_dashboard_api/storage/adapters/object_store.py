"""The object-storage ``Storage`` adapter (``ObjectStorage``), STORAGE.md Section 7.3.

Realizes the full :class:`~...domain.port.Storage` Port over an S3-compatible
object store. The Section 2 tree is a **key-prefix scheme**, not directories
(Section 7.3): the same logical layout — ``communities/<cid>/servers/<sid>/...``,
``jars/<sha>.jar`` — maps to key prefixes, and listings are prefix scans.

Atomic publish is a **pointer-object flip** (Section 4.2): a snapshot is staged
under ``incoming/<transfer>/`` (mirroring the fs layout), then on commit its
objects are server-side copied under a fresh ``snapshots/<sid>/`` prefix and the
single ``current.json`` pointer object is atomically overwritten to reference it.
The pointer PUT is the one atomic step — object stores serve a single-object PUT
last-writer-wins with read-after-write, so ``current.json`` references either the
old or the new snapshot prefix at every instant, never a partial. The superseded
prefix and the staging prefix are garbage-collected after the flip; a crash before
the flip leaves an orphan prefix the sweep reclaims (Section 4.3 object column).

Single-file writes (Section 4.4) mutate the live snapshot prefix: the prior
content is copied into ``versions/`` first (Section 5), then the new object is PUT
under the live snapshot prefix and a fresh pointer is written so the published
state stays named explicitly. There are no symlinks, so the traversal defence
(Section 6) only needs the string-level :class:`RelPath` check plus the key being
confined to the server's prefix.

Wire format mirrors the fs adapter: hydrate/snapshot stream a **tar** of the
working set, backups are self-contained ``tar.gz`` objects. Streaming is
bounded-memory: hydrate egress writes a tar one object-chunk at a time (never a
whole object, let alone the whole working set, in RAM); snapshot ingest spools the
incoming tar to local scratch (bounded disk, like the fs adapter) and uploads each
member via multipart so no whole member is held in memory.

Backup creation (:func:`_write_backup_targz`) is bounded per-member, not
per-chunk: it buffers one object's body whole in memory before adding it to the
gzip stream, then releases it before the next. Peak memory is therefore the size
of the single largest object in the snapshot — never the whole working set, but
not the per-chunk bound the hydrate/ingest paths hold. This is acceptable because
a Minecraft working set's largest individual file is well within memory; if a
deployment ever stores a very large single object, this is the path to tighten.

The S3 calls are confined to the narrow :class:`S3Client` protocol; the concrete
aioboto3 client factory lives in :mod:`.object_client` so the dependency stays at
the very edge and tests run against an in-memory stub.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import json
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

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
from mc_server_dashboard_api.storage.integrity.region import WorkingSetReport

# Egress chunk size for hydrate / JAR streaming (one tar member-chunk at a time).
_CHUNK = 1024 * 1024
# Multipart part size for member upload (the S3 minimum is 5 MiB for non-final
# parts; the stub does not care, but production must respect it).
_PART = 8 * 1024 * 1024
_DEFAULT_VERSION_RETENTION = 10
# Decompressed-size cap for restore extraction. The compressed archive object is
# bounded on the way in, but a gzip member can expand ~1000x; the cumulative
# DECOMPRESSED bytes are counted as members are streamed to staging so a bomb
# cannot fill the store (#287). 8 GiB bounds the amplification while covering a real
# Minecraft world; a constant is intentional (no config knob requested).
_DEFAULT_MAX_RESTORE_BYTES = 8 * 1024 * 1024 * 1024
# The single pointer object naming the live snapshot prefix (Section 4.2).
_POINTER = "current.json"


@dataclass(frozen=True)
class S3Object:
    """One object from a prefix listing: its full key, byte size, and mtime.

    ``last_modified`` is the object's store time (S3 ``LastModified``), the input
    the JAR-pool GC safety window reads (#293); it is timezone-aware UTC.
    """

    key: str
    size: int
    last_modified: dt.datetime


class S3Client(Protocol):
    """The handful of S3 operations the object adapter uses (Section 7.3).

    An async client scoped to one bucket. The concrete aioboto3 implementation
    lives in :mod:`.object_client`; tests provide an in-memory stub. Keeping the
    surface this narrow is what lets the pointer-flip / staged-upload / sweep
    behaviour be proven against a fake without any real cloud.
    """

    async def get_object(self, key: str) -> AsyncIterator[bytes]:
        """Stream an object's body in chunks. Raises :class:`NotFoundError`."""
        ...

    async def put_object(self, key: str, body: bytes) -> None:
        """Write a whole object atomically (single PUT, last-writer-wins)."""
        ...

    async def upload_multipart(self, key: str, parts: AsyncIterator[bytes]) -> None:
        """Write an object from a stream of parts without buffering it whole."""
        ...

    async def head_object(self, key: str) -> int | None:
        """Return the object's size, or ``None`` if it does not exist."""
        ...

    async def copy_object(self, src_key: str, dst_key: str) -> None:
        """Server-side copy one object (no bytes through the API)."""
        ...

    async def delete_object(self, key: str) -> None:
        """Delete one object. Idempotent (no error if absent)."""
        ...

    async def list_objects(self, prefix: str) -> list[S3Object]:
        """List every object whose key starts with ``prefix`` (a prefix scan)."""
        ...


# A factory yielding a per-operation client context manager, so the aioboto3
# session/client lifecycle stays inside the adapter's own calls. An
# ``asynccontextmanager``-decorated async generator satisfies this shape.
S3ClientFactory = Callable[[], AbstractAsyncContextManager[S3Client]]


class _ObjectSnapshotHandle(SnapshotHandle):
    """Names one ``incoming/<transfer>/`` staging prefix for an in-flight snapshot."""

    def __init__(
        self, community_id: CommunityId, server_id: ServerId, transfer_id: str
    ) -> None:
        self.community_id = community_id
        self.server_id = server_id
        self.transfer_id = transfer_id
        # Set true on commit/abort so a reused handle is rejected (protocol safety).
        self.consumed = False


class ObjectStorage(Storage):
    """S3-compatible object-store-backed :class:`Storage` (STORAGE.md Section 7.3).

    ``client_factory`` yields a bucket-scoped :class:`S3Client` per operation.
    ``version_retention`` bounds per-file retained versions (Section 5).
    ``failure_seam`` is the crash-injection hook for the publish-phase tests
    (Section 4.3); production uses the no-op default.

    Active-reader leases (Section 4.2) are adapter-local, like the fs adapter: an
    open hydrate stream pins the snapshot prefix it is reading so a concurrent
    publish/sweep does not GC it out from under the reader.
    """

    def __init__(
        self,
        client_factory: S3ClientFactory,
        *,
        version_retention: int = _DEFAULT_VERSION_RETENTION,
        max_restore_bytes: int = _DEFAULT_MAX_RESTORE_BYTES,
        failure_seam: FailureSeam | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._version_retention = version_retention
        self._max_restore_bytes = max_restore_bytes
        self._seam = failure_seam or FailureSeam()
        self._leases: dict[str, int] = {}
        # Active-staging handles: the ``incoming/<transfer>/`` prefix of each
        # in-flight transfer is held here for the life of its handle (begin ->
        # commit/abort) so a concurrently scheduled sweep does not GC its staging
        # objects out from under the active stream (issue #160). Unlike the fs
        # adapter, object staging has no on-store marker, so this in-process set is
        # the pin; a crash leftover has no in-process handle by definition, so a
        # fresh process's sweep still reclaims it. Guarded by ``_lease_lock``
        # alongside the reader leases.
        self._active_staging: set[str] = set()
        self._lease_lock = threading.Lock()

    # --- key-prefix layout helpers (Section 2 as a key scheme) -------------

    def _server_prefix(self, community_id: CommunityId, server_id: ServerId) -> str:
        return f"communities/{community_id.value}/servers/{server_id.value}/"

    def _pointer_key(self, community_id: CommunityId, server_id: ServerId) -> str:
        return self._server_prefix(community_id, server_id) + _POINTER

    def _snapshot_prefix(
        self, community_id: CommunityId, server_id: ServerId, snapshot_id: str
    ) -> str:
        return (
            self._server_prefix(community_id, server_id) + f"snapshots/{snapshot_id}/"
        )

    def _incoming_prefix(
        self, community_id: CommunityId, server_id: ServerId, transfer_id: str
    ) -> str:
        return self._server_prefix(community_id, server_id) + f"incoming/{transfer_id}/"

    def _versions_prefix(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> str:
        sub = "/".join(rel_path.parts)
        return self._server_prefix(community_id, server_id) + f"versions/{sub}/"

    def _backup_key(
        self, community_id: CommunityId, server_id: ServerId, key: BackupKey
    ) -> str:
        return (
            self._server_prefix(community_id, server_id) + f"backups/{key.value}.tar.gz"
        )

    def _jar_key(self, key: JarKey) -> str:
        return f"jars/{key.sha256}.jar"

    # --- active-reader leases (Section 4.2 reader safety) ------------------

    def _acquire_lease(self, prefix: str) -> None:
        with self._lease_lock:
            self._leases[prefix] = self._leases.get(prefix, 0) + 1

    def _release_lease(self, prefix: str) -> None:
        with self._lease_lock:
            remaining = self._leases.get(prefix, 0) - 1
            if remaining > 0:
                self._leases[prefix] = remaining
            else:
                self._leases.pop(prefix, None)

    def _is_leased(self, prefix: str) -> bool:
        with self._lease_lock:
            return self._leases.get(prefix, 0) > 0

    # --- active-staging leases (issue #160) --------------------------------

    def _register_staging(self, incoming_prefix: str) -> None:
        with self._lease_lock:
            self._active_staging.add(incoming_prefix)

    def _release_staging(self, incoming_prefix: str) -> None:
        with self._lease_lock:
            self._active_staging.discard(incoming_prefix)

    def _is_staging_active(self, incoming_prefix: str) -> bool:
        with self._lease_lock:
            return incoming_prefix in self._active_staging

    # --- pointer object (the publish pointer, Section 4.2) -----------------

    async def _read_pointer(self, client: S3Client, server_prefix: str) -> str | None:
        """Return the live snapshot prefix named by ``current.json``, or ``None``."""

        key = server_prefix + _POINTER
        if await client.head_object(key) is None:
            return None
        raw = await _read_all(client, key)
        value: str = json.loads(raw)["snapshot"]
        return value

    async def _live_snapshot_prefix(
        self, client: S3Client, community_id: CommunityId, server_id: ServerId
    ) -> str:
        prefix = await self._read_pointer(
            client, self._server_prefix(community_id, server_id)
        )
        if prefix is None:
            raise NotFoundError(f"no published snapshot for server {server_id.value}")
        return prefix

    # --- path-traversal containment (Section 6) ----------------------------

    def _safe_subkey(self, rel_path: RelPath) -> str:
        """Derive the key suffix from ``rel_path``, confined to the server prefix.

        ``RelPath`` already rejected absolute paths and ``..`` at the string level
        (the only vector on object storage — there are no symlinks). Re-checking the
        normalised result keeps the containment guarantee explicit (Section 6): a
        suffix that would escape its prefix is refused, never silently clamped.
        """

        sub = "/".join(rel_path.parts)
        if sub.startswith("/") or ".." in PurePosixPath(sub or ".").parts:
            raise PathTraversalError(
                f"rel_path {rel_path.value!r} escapes the server prefix"
            )
        return sub

    # --- crash-recovery sweep (Section 4.3 object column) ------------------

    async def sweep(self) -> None:
        """GC orphaned staging + superseded snapshot prefixes, idempotently.

        Keyed off each server's live pointer (Section 4.3): every object under a
        ``snapshots/<id>/`` prefix the pointer does not name is unreferenced and is
        deleted, and every ``incoming/`` object is leftover and is deleted. Safe to
        re-run; never touches the prefix the pointer resolves to. A superseded
        prefix an active hydrate reader still leases is skipped and reclaimed by a
        later sweep once the reader releases (Section 4.2 reader safety).

        Sweep-vs-flip race (issue #113): a concurrent publish whose new-prefix
        objects were already in the listing but whose pointer flip lands after the
        per-server pointer read would otherwise see the just-made-live prefix as an
        orphan and delete it. The guard below re-reads the pointer immediately
        before deleting each candidate snapshot prefix and skips it if the pointer
        now names it. This narrows the window to the gap between that re-read and
        the delete; it does not eliminate it, so the sweep remains safest run when
        no publisher is concurrent (today: API startup only — Section 8.5 leaves
        scheduling open).

        In-flight staging (issue #160): a transfer staged but not yet committed has
        no live pointer to re-read against, so it is pinned instead by an in-process
        active-staging lease taken at ``begin_snapshot`` and released at
        commit/abort. The ``incoming/`` branch below skips any prefix whose lease is
        still active, so a sweep scheduled concurrently with an in-flight stage
        leaves its staging objects intact. A crash leftover has no in-process handle
        by definition, so a fresh process's sweep still reclaims it.
        """

        async with self._client_factory() as client:
            objs = await client.list_objects("communities/")
            for server_prefix, keys in _group_by_server(objs).items():
                await self._sweep_server(client, server_prefix, keys)

    async def _sweep_server(
        self, client: S3Client, server_prefix: str, keys: list[str]
    ) -> None:
        # One re-read decision per candidate snapshot prefix (not per key): the
        # first object encountered under a prefix re-reads the pointer and caches
        # whether to delete that whole prefix.
        delete_prefix: dict[str, bool] = {}
        for key in keys:
            rest = key[len(server_prefix) :]
            if rest.startswith("incoming/"):
                # Skip an in-flight transfer's staging objects: the prefix is pinned
                # by an active-staging lease until commit/abort (issue #160). A crash
                # leftover has no in-process handle, so it is not skipped.
                incoming_prefix = server_prefix + "/".join(rest.split("/")[:2]) + "/"
                if not self._is_staging_active(incoming_prefix):
                    await client.delete_object(key)
            elif rest.startswith("snapshots/"):
                snap_prefix = server_prefix + "/".join(rest.split("/")[:2]) + "/"
                if snap_prefix not in delete_prefix:
                    # Re-read the pointer immediately before the first delete under
                    # this prefix: a concurrent publish may have flipped it onto the
                    # candidate after the listing was taken (issue #113), in which
                    # case the prefix is live now and must be kept. A leased
                    # superseded prefix is likewise kept (Section 4.2).
                    live = await self._read_pointer(client, server_prefix)
                    delete_prefix[snap_prefix] = (
                        snap_prefix != live and not self._is_leased(snap_prefix)
                    )
                if delete_prefix[snap_prefix]:
                    await client.delete_object(key)

    # --- working-set hydrate / snapshot (Section 3.1) ----------------------

    def open_hydrate_source(
        self, community_id: CommunityId, server_id: ServerId
    ) -> ByteStream:
        # The live snapshot is resolved and leased on the FIRST iteration (not at
        # open time), mirroring the fs adapter: a stream opened but never iterated
        # never pins a prefix, and the leased prefix is exactly the one streamed
        # (Section 4.2 reader safety).
        return self._hydrate_gen(community_id, server_id)

    async def _hydrate_gen(
        self, community_id: CommunityId, server_id: ServerId
    ) -> AsyncIterator[bytes]:
        async with self._client_factory() as client:
            snapshot_prefix = await self._live_snapshot_prefix(
                client, community_id, server_id
            )
            self._acquire_lease(snapshot_prefix)
            try:
                members = sorted(
                    await client.list_objects(snapshot_prefix), key=lambda o: o.key
                )
                async for chunk in _tar_stream_from_objects(
                    client, snapshot_prefix, members
                ):
                    yield chunk
            finally:
                self._release_lease(snapshot_prefix)

    async def begin_snapshot(
        self, community_id: CommunityId, server_id: ServerId
    ) -> SnapshotHandle:
        # No prefix to pre-create on object storage; objects appear on first PUT.
        # Register the staging prefix as active so a concurrent sweep skips its
        # incoming/ objects until commit/abort releases it (issue #160).
        transfer_id = uuid.uuid4().hex
        self._register_staging(
            self._incoming_prefix(community_id, server_id, transfer_id)
        )
        return _ObjectSnapshotHandle(community_id, server_id, transfer_id)

    async def write_snapshot(self, handle: SnapshotHandle, stream: ByteStream) -> None:
        h = _as_object_handle(handle)
        if h.consumed:
            raise SnapshotHandleError("snapshot handle already committed or aborted")
        incoming = self._incoming_prefix(h.community_id, h.server_id, h.transfer_id)
        # Spool the incoming tar to local scratch (bounded disk, like the fs
        # adapter), then upload each member as its own object via multipart so no
        # whole member is held in RAM. ``..``/absolute members are refused, the
        # tar-side traversal defence.
        async with self._client_factory() as client:
            spool = await _spool_stream(stream, ".snapshot.", ".tar")
            try:
                for name in await asyncio.to_thread(
                    _safe_archive_members, spool, "r:*"
                ):
                    await client.upload_multipart(
                        incoming + name, _archive_member_parts(spool, name, "r:*")
                    )
            finally:
                await asyncio.to_thread(spool.unlink, missing_ok=True)

    async def commit_snapshot(self, handle: SnapshotHandle) -> None:
        h = _as_object_handle(handle)
        if h.consumed:
            raise SnapshotHandleError("snapshot handle already committed or aborted")
        incoming = self._incoming_prefix(h.community_id, h.server_id, h.transfer_id)
        async with self._client_factory() as client:
            staged = await client.list_objects(incoming)
            if not staged:
                # The "proven complete" gate (Section 4.1): an empty staging area is
                # not a publishable transfer. The end-of-stream + manifest/size
                # integrity check is part of the data-plane contract (epic #8); this
                # is the gate that will host it.
                raise IncompleteTransferError("no staged objects to publish")
            await self._publish(client, h.community_id, h.server_id, incoming, staged)
        # Publish reclaimed the staging prefix; release its active-staging lease so
        # a later sweep is not blocked by a now-dead handle (issue #160).
        self._release_staging(incoming)
        h.consumed = True

    async def abort_snapshot(self, handle: SnapshotHandle) -> None:
        h = _as_object_handle(handle)
        incoming = self._incoming_prefix(h.community_id, h.server_id, h.transfer_id)
        async with self._client_factory() as client:
            await _delete_prefix(client, incoming)
        self._release_staging(incoming)
        h.consumed = True

    async def _publish(
        self,
        client: S3Client,
        community_id: CommunityId,
        server_id: ServerId,
        staged_prefix: str,
        staged: list[S3Object],
    ) -> None:
        """The pointer-flip publish core (Section 4.2 object column).

        Steps, each followed by a failure-seam boundary so a crash at any of them
        leaves the pointer resolving to one complete snapshot (Section 4.3 object
        column): copy staged objects under a fresh ``snapshots/<id>/`` prefix; PUT
        the new pointer object (the atomic flip); GC the superseded prefix and the
        staging prefix.
        """

        self._seam.reach(PublishPhase.AFTER_STAGE)

        snapshot_id = SnapshotId.new()
        new_prefix = self._snapshot_prefix(community_id, server_id, snapshot_id.value)
        for obj in staged:
            rest = obj.key[len(staged_prefix) :]
            await client.copy_object(obj.key, new_prefix + rest)

        self._seam.reach(PublishPhase.AFTER_MOVE)

        server_prefix = self._server_prefix(community_id, server_id)
        old_prefix = await self._read_pointer(client, server_prefix)
        # The single atomic step: overwrite the one pointer object. After it returns
        # the pointer references either the old or the new prefix, never a partial.
        await client.put_object(
            server_prefix + _POINTER,
            json.dumps({"snapshot": new_prefix}).encode(),
        )

        self._seam.reach(PublishPhase.AFTER_FLIP)

        # Reclaim the staging prefix and, unless an active reader leases it, the
        # superseded snapshot prefix (Section 4.2/4.3). A leased old prefix is left
        # for the next sweep to reclaim once the reader releases.
        await _delete_prefix(client, staged_prefix)
        if (
            old_prefix is not None
            and old_prefix != new_prefix
            and not self._is_leased(old_prefix)
        ):
            await _delete_prefix(client, old_prefix)

    # --- JAR store / reuse (Section 3.2) -----------------------------------

    async def put_jar(self, stream: ByteStream) -> JarKey:
        # Spool to local scratch, hashing as we go, then PUT under the content key.
        # Identical bytes land on the same key (idempotent dedup, Section 3.2).
        hasher = hashlib.sha256()
        spool = await _spool_stream(stream, ".jar.", ".tmp", hasher=hasher)
        try:
            key = JarKey(hasher.hexdigest())
            async with self._client_factory() as client:
                if await client.head_object(self._jar_key(key)) is None:
                    await client.upload_multipart(
                        self._jar_key(key), _file_parts(spool)
                    )
            return key
        finally:
            await asyncio.to_thread(spool.unlink, missing_ok=True)

    async def has_jar(self, key: JarKey) -> bool:
        async with self._client_factory() as client:
            return await client.head_object(self._jar_key(key)) is not None

    def open_jar(self, key: JarKey) -> ByteStream:
        return self._jar_gen(key)

    async def _jar_gen(self, key: JarKey) -> AsyncIterator[bytes]:
        async with self._client_factory() as client:
            if await client.head_object(self._jar_key(key)) is None:
                raise NotFoundError(f"jar not found: {key.sha256}")
            async for chunk in await client.get_object(self._jar_key(key)):
                yield chunk

    async def jar_pool_stats(self) -> JarPoolStats:
        # One prefix scan over the content-addressed ``jars/`` namespace (Section
        # 2 as a key scheme); each object's size comes back with the listing.
        async with self._client_factory() as client:
            objs = await client.list_objects("jars/")
        return JarPoolStats(count=len(objs), total_bytes=sum(obj.size for obj in objs))

    async def list_jars(self) -> list[JarPoolEntry]:
        # Same ``jars/`` prefix scan as jar_pool_stats; each object's size + mtime
        # come back with the listing, the GC's reference-diff + safety-window input
        # (#293). The content key is the basename with the ``.jar`` suffix stripped.
        async with self._client_factory() as client:
            objs = await client.list_objects("jars/")
        return [
            JarPoolEntry(
                key=JarKey(obj.key.removeprefix("jars/").removesuffix(".jar")),
                size_bytes=obj.size,
                modified_at=obj.last_modified,
            )
            for obj in objs
        ]

    async def delete_jar(self, key: JarKey) -> None:
        # delete_object is idempotent (no error if absent), matching the Port.
        async with self._client_factory() as client:
            await client.delete_object(self._jar_key(key))

    # --- backup archive create / list / restore / delete (Section 3.3) -----

    async def create_backup_from_current(
        self, community_id: CommunityId, server_id: ServerId
    ) -> BackupKey:
        key = BackupKey(uuid.uuid4().hex)
        async with self._client_factory() as client:
            snapshot_prefix = await self._live_snapshot_prefix(
                client, community_id, server_id
            )
            objs = sorted(
                await client.list_objects(snapshot_prefix), key=lambda o: o.key
            )
            # Build the self-contained tar.gz to local scratch (gzip streams, so the
            # bodies are not all held at once), then upload it as one object.
            fd, spool_name = await asyncio.to_thread(
                tempfile.mkstemp, prefix=".backup.", suffix=".tar.gz"
            )
            await asyncio.to_thread(_close_fd, fd)
            spool = Path(spool_name)
            try:
                await _write_backup_targz(client, snapshot_prefix, objs, spool)
                await client.upload_multipart(
                    self._backup_key(community_id, server_id, key), _file_parts(spool)
                )
            finally:
                await asyncio.to_thread(spool.unlink, missing_ok=True)
        return key

    async def list_backups(
        self, community_id: CommunityId, server_id: ServerId
    ) -> list[BackupKey]:
        prefix = self._server_prefix(community_id, server_id) + "backups/"
        async with self._client_factory() as client:
            objs = await client.list_objects(prefix)
        keys = []
        for obj in sorted(objs, key=lambda o: o.key):
            name = obj.key[len(prefix) :]
            if name.endswith(".tar.gz"):
                keys.append(BackupKey(name[: -len(".tar.gz")]))
        return keys

    async def restore_backup(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        key: BackupKey,
        *,
        force: bool = False,
    ) -> WorkingSetReport:
        # The restore-direction integrity gate (issue #743) is fs-only: it walks a
        # local working-set directory (issue #738), which the object backend does
        # not stage — it streams members straight to object storage. Like the
        # create-direction gate (#749), the object adapter is ungated, so ``force``
        # is a no-op here and a healthy report is returned to satisfy the Port.
        del force
        backup_key = self._backup_key(community_id, server_id, key)
        transfer_id = f"restore-{key.value}-{uuid.uuid4().hex}"
        incoming = self._incoming_prefix(community_id, server_id, transfer_id)
        # A restore stages under incoming/ exactly like a snapshot, so pin it with
        # the same active-staging lease for the life of the operation (issue #160).
        self._register_staging(incoming)
        try:
            async with self._client_factory() as client:
                if await client.head_object(backup_key) is None:
                    raise NotFoundError(f"backup not found: {key.value}")
                # Stage the extracted archive under the incoming prefix, then publish
                # it through the same pointer-flip path as a snapshot (Section 4.1).
                spool = await _spool_object(client, backup_key, ".restore.", ".tar.gz")
                try:
                    # Count cumulative DECOMPRESSED bytes across members: a gzip
                    # member can expand ~1000x past the compressed object, so the cap
                    # aborts a bomb before it fills the store (#287).
                    budget = _RestoreBudget(self._max_restore_bytes)
                    for name in await asyncio.to_thread(
                        _safe_archive_members, spool, "r:gz"
                    ):
                        await client.upload_multipart(
                            incoming + name,
                            budget.count(_archive_member_parts(spool, name, "r:gz")),
                        )
                    staged = await client.list_objects(incoming)
                    await self._publish(
                        client, community_id, server_id, incoming, staged
                    )
                except BaseException:
                    await _delete_prefix(client, incoming)
                    raise
                finally:
                    await asyncio.to_thread(spool.unlink, missing_ok=True)
        finally:
            self._release_staging(incoming)
        return WorkingSetReport()

    async def delete_backup(
        self, community_id: CommunityId, server_id: ServerId, key: BackupKey
    ) -> None:
        async with self._client_factory() as client:
            await client.delete_object(self._backup_key(community_id, server_id, key))

    def open_backup(
        self, community_id: CommunityId, server_id: ServerId, key: BackupKey
    ) -> ByteStream:
        return self._backup_gen(community_id, server_id, key)

    async def _backup_gen(
        self, community_id: CommunityId, server_id: ServerId, key: BackupKey
    ) -> AsyncIterator[bytes]:
        backup_key = self._backup_key(community_id, server_id, key)
        async with self._client_factory() as client:
            if await client.head_object(backup_key) is None:
                raise NotFoundError(f"backup not found: {key.value}")
            # Stream the stored archive object verbatim (no recompression): the
            # object is already a self-contained tar.gz (issue #281).
            async for chunk in await client.get_object(backup_key):
                yield chunk

    async def put_backup(
        self, community_id: CommunityId, server_id: ServerId, stream: ByteStream
    ) -> BackupKey:
        # Store the uploaded archive bytes verbatim under a fresh key (the caller
        # already validated the archive). A single multipart upload makes the new
        # object appear atomically, so a partial upload never lists as a backup
        # (issue #281).
        key = BackupKey(uuid.uuid4().hex)
        async with self._client_factory() as client:
            await client.upload_multipart(
                self._backup_key(community_id, server_id, key), stream
            )
        return key

    async def backup_size(
        self, community_id: CommunityId, server_id: ServerId, key: BackupKey
    ) -> int:
        backup_key = self._backup_key(community_id, server_id, key)
        async with self._client_factory() as client:
            size = await client.head_object(backup_key)
        if size is None:
            raise NotFoundError(f"backup not found: {key.value}")
        return size

    # --- file read / edit on the authoritative copy (Section 3.4) ----------

    async def read_file(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> bytes:
        sub = self._safe_subkey(rel_path)
        async with self._client_factory() as client:
            snapshot_prefix = await self._live_snapshot_prefix(
                client, community_id, server_id
            )
            key = snapshot_prefix + sub
            if await client.head_object(key) is None:
                raise NotFoundError(f"file not found: {rel_path.value}")
            return await _read_all(client, key)

    def open_file_stream(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> ByteStream:
        # The per-file analogue of open_hydrate_source (issue #265): a streamed
        # GET of one object so a large single-file download never buffers the
        # whole object in RAM. The live snapshot prefix is resolved and the
        # active-reader lease taken on the FIRST iteration, mirroring the hydrate
        # stream: a stream opened but never iterated never pins a prefix, and the
        # leased prefix is exactly the one the object is read out of (Section 4.2
        # reader safety).
        sub = self._safe_subkey(rel_path)
        return self._file_stream_gen(community_id, server_id, sub, rel_path)

    async def _file_stream_gen(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        sub: str,
        rel_path: RelPath,
    ) -> AsyncIterator[bytes]:
        async with self._client_factory() as client:
            snapshot_prefix = await self._live_snapshot_prefix(
                client, community_id, server_id
            )
            self._acquire_lease(snapshot_prefix)
            try:
                key = snapshot_prefix + sub
                if await client.head_object(key) is None:
                    raise NotFoundError(f"file not found: {rel_path.value}")
                async for chunk in await client.get_object(key):
                    yield chunk
            finally:
                self._release_lease(snapshot_prefix)

    async def list_dir(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> list[DirEntry]:
        sub = self._safe_subkey(rel_path)
        dir_suffix = sub + "/" if sub else ""
        async with self._client_factory() as client:
            server_prefix = self._server_prefix(community_id, server_id)
            snapshot_prefix = await self._read_pointer(client, server_prefix)
            # A never-snapshotted server has an empty working set, not a missing one
            # (issue #205): list it as empty rather than raising, mirroring the data
            # plane's JAR-only hydrate posture for the unpublished state.
            if snapshot_prefix is None:
                return []
            objs = await client.list_objects(snapshot_prefix + dir_suffix)
        if not objs and sub:
            # A prefix with no members is an empty (non-existent) directory; the
            # root (sub == "") always lists, even when empty.
            raise NotFoundError(f"directory not found: {rel_path.value}")
        return _entries_at_level(objs, snapshot_prefix + dir_suffix)

    async def write_file(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: RelPath,
        data: bytes,
    ) -> None:
        sub = self._safe_subkey(rel_path)
        # The working-set root (an empty rel_path, e.g. the file route's default
        # ``path="."``) names a directory, not a file; refuse it rather than
        # writing a bogus empty-suffix object onto the snapshot prefix (the at-rest
        # 500 parity with the fs backend, issue #542).
        if not sub:
            raise PathTraversalError("rel_path must name a file, not the root")
        async with self._client_factory() as client:
            server_prefix = self._server_prefix(community_id, server_id)
            snapshot_prefix = await self._read_pointer(client, server_prefix)
            if snapshot_prefix is None:
                # A never-snapshotted server has no live prefix to edit in place
                # (issue #205). Initialize the first published version containing
                # just this file through the same pointer-flip publish path a
                # snapshot uses, so a concurrent snapshot publish (its own staging
                # prefix + flip) cannot corrupt it (Section 4.2).
                await self._publish_initial(client, community_id, server_id, sub, data)
                return
            key = snapshot_prefix + sub
            # Refuse to overwrite a directory with file bytes (issue #542): a
            # ``config`` write where ``config/...`` objects exist would create a
            # file/prefix collision. The fs backend raises IsADirectoryError here;
            # match it with the invalid-path refusal.
            if await client.list_objects(key + "/"):
                raise PathTraversalError(
                    f"rel_path names a directory, not a file: {rel_path.value}"
                )
            # Capture the prior version BEFORE overwriting (Section 4.4/5).
            if await client.head_object(key) is not None:
                await self._capture_version(
                    client, community_id, server_id, rel_path, key
                )
            self._seam.reach(PublishPhase.AFTER_VERSION_CAPTURE)
            # PUT the new object, then re-write the pointer so the published state
            # stays named explicitly; the pointer PUT is the atomic point (Section 4.4).
            await client.put_object(key, data)
            self._seam.reach(PublishPhase.AFTER_FILE_TEMP_WRITE)
            await client.put_object(
                self._pointer_key(community_id, server_id),
                json.dumps({"snapshot": snapshot_prefix}).encode(),
            )

    async def _publish_initial(
        self,
        client: S3Client,
        community_id: CommunityId,
        server_id: ServerId,
        sub: str,
        data: bytes,
    ) -> None:
        """Publish the first version of a never-snapshotted server (issue #205).

        Stage just ``sub`` under a fresh ``incoming/`` prefix, then publish it
        through :meth:`_publish` — the same pointer-flip path a snapshot commit
        uses. The staging prefix is pinned with an active-staging lease for the
        life of the operation so a concurrent sweep does not GC it (issue #160).
        """

        incoming = self._incoming_prefix(community_id, server_id, uuid.uuid4().hex)
        self._register_staging(incoming)
        try:
            await client.put_object(incoming + sub, data)
            staged = await client.list_objects(incoming)
            await self._publish(client, community_id, server_id, incoming, staged)
        except BaseException:
            await _delete_prefix(client, incoming)
            raise
        finally:
            self._release_staging(incoming)

    async def delete_file(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        sub = self._safe_subkey(rel_path)
        async with self._client_factory() as client:
            snapshot_prefix = await self._live_snapshot_prefix(
                client, community_id, server_id
            )
            key = snapshot_prefix + sub
            if await client.head_object(key) is None:
                raise NotFoundError(f"file not found: {rel_path.value}")
            # Capture the content BEFORE removing it (Section 5), so a delete is
            # reversible by rollback exactly like an overwrite is.
            await self._capture_version(client, community_id, server_id, rel_path, key)
            self._seam.reach(PublishPhase.AFTER_VERSION_CAPTURE)
            await client.delete_object(key)
            # Re-write the pointer so the published state stays named explicitly,
            # mirroring write_file's post-mutation pointer rewrite (Section 4.4).
            await client.put_object(
                self._pointer_key(community_id, server_id),
                json.dumps({"snapshot": snapshot_prefix}).encode(),
            )

    async def delete_dir(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        sub = self._safe_subkey(rel_path)
        dir_suffix = sub + "/" if sub else ""
        async with self._client_factory() as client:
            snapshot_prefix = await self._live_snapshot_prefix(
                client, community_id, server_id
            )
            objs = await client.list_objects(snapshot_prefix + dir_suffix)
            if not objs:
                raise NotFoundError(f"directory not found: {rel_path.value}")
            # No per-file version capture (Port contract): whole-subtree recovery
            # is the backups' job (Section 3.3). Delete every object under the dir
            # prefix, then re-write the pointer to keep the state named explicitly.
            for obj in objs:
                await client.delete_object(obj.key)
            await client.put_object(
                self._pointer_key(community_id, server_id),
                json.dumps({"snapshot": snapshot_prefix}).encode(),
            )

    async def make_dir(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        # Object storage has no real directories — a directory exists only as the
        # shared key-prefix of its files (Section 7.3), so an empty directory
        # cannot be represented and make_dir is a no-op (the documented limitation,
        # issue #259). Validate the path so a traversal-unsafe input is still
        # rejected at the seam rather than silently accepted.
        self._safe_subkey(rel_path)

    # --- file version retention / rollback (Section 3.5, Section 5) ---------

    async def _capture_version(
        self,
        client: S3Client,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: RelPath,
        source_key: str,
    ) -> None:
        versions = self._versions_prefix(community_id, server_id, rel_path)
        await client.copy_object(source_key, versions + _new_version_id())
        await self._prune_versions(client, versions)

    async def _prune_versions(self, client: S3Client, versions: str) -> None:
        objs = sorted(await client.list_objects(versions), key=lambda o: o.key)
        excess = len(objs) - self._version_retention
        for obj in objs[:excess] if excess > 0 else []:
            await client.delete_object(obj.key)

    async def retain_file_version(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        # Retain the current authoritative object as a version unless it already
        # equals the newest retained version (issue #351): a running edit snapshots
        # the frozen authoritative copy before each edit, and without this dedup
        # repeated edits to one file would push identical copies into the bounded
        # ring and evict distinct at-rest versions.
        sub = self._safe_subkey(rel_path)
        async with self._client_factory() as client:
            snapshot_prefix = await self._read_pointer(
                client, self._server_prefix(community_id, server_id)
            )
            if snapshot_prefix is None:
                return  # never-published server: nothing authoritative to retain
            key = snapshot_prefix + sub
            if await client.head_object(key) is None:
                return  # no authoritative copy yet: nothing to retain
            versions = self._versions_prefix(community_id, server_id, rel_path)
            if await self._matches_newest_version(client, versions, key):
                return  # unchanged since the newest retained version: skip churn
            await self._capture_version(client, community_id, server_id, rel_path, key)

    async def _matches_newest_version(
        self, client: S3Client, versions: str, source_key: str
    ) -> bool:
        """True if ``source_key`` equals the newest retained version under ``versions``.

        Compares by size first (a cheap HEAD reject), then by SHA-256 of the
        streamed bodies so two large identical objects are hashed independently
        rather than both held in memory.
        """

        objs = sorted(await client.list_objects(versions), key=lambda o: o.key)
        if not objs:
            return False
        newest = objs[-1].key  # ids are time-ordered (_new_version_id)
        source_size = await client.head_object(source_key)
        if source_size is None or source_size != objs[-1].size:
            return False
        return await _stream_sha256(client, source_key) == await _stream_sha256(
            client, newest
        )

    async def list_file_versions(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> list[VersionId]:
        versions = self._versions_prefix(community_id, server_id, rel_path)
        async with self._client_factory() as client:
            objs = sorted(await client.list_objects(versions), key=lambda o: o.key)
        # Newest-first (Section 3.5); version ids are time-ordered (_new_version_id).
        return [VersionId(obj.key[len(versions) :]) for obj in reversed(objs)]

    async def read_file_version(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: RelPath,
        version_id: VersionId,
    ) -> bytes:
        versions = self._versions_prefix(community_id, server_id, rel_path)
        key = versions + version_id.value
        async with self._client_factory() as client:
            if await client.head_object(key) is None:
                raise NotFoundError(f"version not found: {version_id.value}")
            return await _read_all(client, key)

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


# --- module-level helpers ---------------------------------------------------


def _as_object_handle(handle: SnapshotHandle) -> _ObjectSnapshotHandle:
    if not isinstance(handle, _ObjectSnapshotHandle):
        raise SnapshotHandleError("handle was not issued by this adapter")
    return handle


def _new_version_id() -> str:
    """A chronologically sortable, collision-resistant version id (Section 5).

    Same scheme as the fs adapter: a zero-padded nanosecond timestamp so
    lexicographic order equals creation order, with a short random suffix to
    de-collide ids minted in the same nanosecond.
    """

    return f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}"


def _close_fd(fd: int) -> None:
    import os

    os.close(fd)


def _group_by_server(objs: list[S3Object]) -> dict[str, list[str]]:
    """Bucket listing keys by their ``communities/<cid>/servers/<sid>/`` prefix."""

    groups: dict[str, list[str]] = {}
    for obj in objs:
        parts = obj.key.split("/")
        if len(parts) < 5:
            continue
        server_prefix = "/".join(parts[:4]) + "/"
        groups.setdefault(server_prefix, []).append(obj.key)
    return groups


async def _read_all(client: S3Client, key: str) -> bytes:
    return b"".join([chunk async for chunk in await client.get_object(key)])


async def _stream_sha256(client: S3Client, key: str) -> str:
    """SHA-256 of an object, hashed over its streamed body (never buffered whole)."""

    hasher = hashlib.sha256()
    async for chunk in await client.get_object(key):
        hasher.update(chunk)
    return hasher.hexdigest()


async def _delete_prefix(client: S3Client, prefix: str) -> None:
    """Delete every object under ``prefix``. Idempotent (empty prefix is a no-op)."""

    for obj in await client.list_objects(prefix):
        await client.delete_object(obj.key)


def _entries_at_level(objs: list[S3Object], dir_prefix: str) -> list[DirEntry]:
    """Collapse a prefix scan into one directory level's entries (Section 3.4).

    Keys directly under ``dir_prefix`` are files; keys with a further ``/`` are a
    subdirectory, reported once with size 0 (object stores have no directory size).
    """

    files: dict[str, int] = {}
    dirs: set[str] = set()
    for obj in objs:
        rest = obj.key[len(dir_prefix) :]
        if "/" in rest:
            dirs.add(rest.split("/", 1)[0])
        else:
            files[rest] = obj.size
    entries = [DirEntry(name=name, is_dir=True, size=0) for name in dirs]
    entries += [
        DirEntry(name=name, is_dir=False, size=size) for name, size in files.items()
    ]
    return sorted(entries, key=lambda e: e.name)


async def _spool_stream(
    stream: ByteStream,
    prefix: str,
    suffix: str,
    *,
    hasher: Any | None = None,
) -> Path:
    """Spool an async byte stream to a local temp file (bounded disk), optionally
    hashing as it lands. Returns the spool path; the caller unlinks it."""

    fd, name = await asyncio.to_thread(tempfile.mkstemp, prefix=prefix, suffix=suffix)
    with open(fd, "wb") as out:
        async for chunk in stream:
            if hasher is not None:
                hasher.update(chunk)
            await asyncio.to_thread(out.write, chunk)
    return Path(name)


async def _spool_object(client: S3Client, key: str, prefix: str, suffix: str) -> Path:
    """Spool one object's body to a local temp file (bounded disk)."""

    fd, name = await asyncio.to_thread(tempfile.mkstemp, prefix=prefix, suffix=suffix)
    with open(fd, "wb") as out:
        async for chunk in await client.get_object(key):
            await asyncio.to_thread(out.write, chunk)
    return Path(name)


# --- tar streaming (bounded memory, one member-chunk at a time) -------------


async def _tar_stream_from_objects(
    client: S3Client, snapshot_prefix: str, members: list[S3Object]
) -> AsyncIterator[bytes]:
    """Yield a tar of the snapshot objects, streaming each body chunk-by-chunk.

    A custom streaming writer (header + padded body per member, then the two-block
    end marker) is used instead of holding any whole object: peak memory is one
    ``_CHUNK`` plus a 512-byte header, never a whole object or the working set.
    """

    blocksize = tarfile.BLOCKSIZE
    for obj in members:
        name = obj.key[len(snapshot_prefix) :]
        info = tarfile.TarInfo(name=name)
        info.size = obj.size
        yield info.tobuf(format=tarfile.GNU_FORMAT)
        written = 0
        async for chunk in await client.get_object(obj.key):
            written += len(chunk)
            yield chunk
        remainder = written % blocksize
        if remainder:
            yield b"\0" * (blocksize - remainder)
    # The end-of-archive marker: two zero blocks.
    yield b"\0" * (blocksize * 2)


def _safe_archive_members(spool: Path, mode: Literal["r:*", "r:gz"]) -> list[str]:
    """List the regular-file member names of the spooled (tar / tar.gz) archive,
    refusing ``..``/absolute names (the tar-side traversal defence, matching the
    fs adapter's ``filter="data"`` posture) so a hostile archive cannot place
    objects outside the server's key prefix."""

    names: list[str] = []
    with tarfile.open(str(spool), mode=mode) as tar:
        for member in tar.getmembers():
            if member.isfile():
                pure = PurePosixPath(member.name)
                if pure.is_absolute() or ".." in pure.parts:
                    raise PathTraversalError(
                        f"tar member escapes the server prefix: {member.name!r}"
                    )
                names.append(member.name)
    return names


class _RestoreBudget:
    """Tally decompressed bytes across a restore's members, bounding the total.

    The running sum is checked after every chunk, so a single high-ratio member
    aborts mid-stream (:class:`ArchiveTooLargeError`) rather than being uploaded to
    staging in full first — the gzip-bomb defence (#287). The count is over actual
    bytes read, not the forgeable member header.
    """

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._total = 0

    async def count(self, parts: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        async for chunk in parts:
            self._total += len(chunk)
            if self._total > self._max_bytes:
                raise ArchiveTooLargeError(
                    f"restore archive exceeds {self._max_bytes} decompressed bytes"
                )
            yield chunk


async def _archive_member_parts(
    spool: Path, name: str, mode: Literal["r:*", "r:gz"]
) -> AsyncIterator[bytes]:
    """Stream one archive member's bytes in part-sized chunks (bounded memory)."""

    tar, member_file = await asyncio.to_thread(_open_member, spool, name, mode)
    try:
        while True:
            chunk = await asyncio.to_thread(member_file.read, _PART)
            if not chunk:
                return
            yield chunk
    finally:
        await asyncio.to_thread(tar.close)


def _open_member(
    spool: Path, name: str, mode: Literal["r:*", "r:gz"]
) -> tuple[tarfile.TarFile, Any]:
    tar = tarfile.open(str(spool), mode=mode)
    member_file = tar.extractfile(name)
    if member_file is None:  # pragma: no cover - members are pre-filtered to files
        tar.close()
        raise NotFoundError(f"tar member not found: {name!r}")
    return tar, member_file


async def _file_parts(spool: Path) -> AsyncIterator[bytes]:
    """Stream a local spool file in part-sized chunks (bounded memory)."""

    handle = await asyncio.to_thread(open, spool, "rb")
    try:
        while True:
            chunk = await asyncio.to_thread(handle.read, _PART)
            if not chunk:
                return
            yield chunk
    finally:
        await asyncio.to_thread(handle.close)


async def _write_backup_targz(
    client: S3Client,
    snapshot_prefix: str,
    objs: list[S3Object],
    spool: Path,
) -> None:
    """Write a self-contained ``tar.gz`` of the snapshot objects to ``spool``."""

    tar = await asyncio.to_thread(tarfile.open, str(spool), "w:gz")
    try:
        for obj in objs:
            name = obj.key[len(snapshot_prefix) :]
            info = tarfile.TarInfo(name=name)
            info.size = obj.size
            buf = io.BytesIO()
            async for chunk in await client.get_object(obj.key):
                buf.write(chunk)
            buf.seek(0)
            await asyncio.to_thread(tar.addfile, info, buf)
    finally:
        await asyncio.to_thread(tar.close)
