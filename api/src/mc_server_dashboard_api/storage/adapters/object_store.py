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
import logging
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
    IntegrityCheckError,
    MissingRegionsError,
    NotFoundError,
    PathTraversalError,
    SnapshotHandleError,
    StaleGenerationError,
)
from mc_server_dashboard_api.storage.domain.port import (
    API_EDIT_PUBLISHER,
    RESTORE_PUBLISHER,
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
from mc_server_dashboard_api.storage.integrity.region import (
    RegionFinding,
    WorkingSetReport,
    check_region_bytes,
    compare_region_name_sets,
)

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
# The single marker object holding the working-set generation AND the publishing
# Worker id (issues #763, #847): line 1 is the generation ``commit_snapshot`` bumps
# (and the hydrate data plane stamps onto a transfer), line 2 (optional) is the
# Worker id that published ``current``. The publish-time generation guard reads the
# publisher to allow a same-Worker re-publish (lost-response self-heal) while
# refusing a different-Worker stale publish (A->B->A). Both live in ONE object so a
# single atomic PUT keeps the (generation, publisher) pair consistent — a crash
# between two separate writes could attribute the previous publisher to the new
# generation and invert the guard.
_GENERATION = "generation"
# Zero-byte marker object placed inside a directory prefix by ``make_dir`` so the
# otherwise-empty prefix is visible in listings (issue #1125).
_DIR_MARKER = ".dir"
# Age threshold below which an in-progress multipart upload is left alone by the
# sweep (issue #903): only uploads initiated more than this long ago are aborted,
# so a live ``put_backup``/``upload_multipart`` is never aborted out from under
# itself. Mirrors the fs adapter's spool-sweep age guard.
_MULTIPART_SWEEP_MIN_AGE_S = 3600

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class S3Object:
    """One object from a prefix listing: its full key, byte size, and mtime.

    ``last_modified`` is the object's store time (S3 ``LastModified``), the input
    the JAR-pool GC safety window reads (#293); it is timezone-aware UTC.
    """

    key: str
    size: int
    last_modified: dt.datetime


@dataclass(frozen=True)
class S3MultipartUpload:
    """One in-progress multipart upload from a ListMultipartUploads scan (issue #903).

    ``initiated`` is the S3 ``Initiated`` timestamp (timezone-aware UTC); the sweep
    reads it to abort only uploads older than its age threshold, so a live upload is
    never aborted out from under ``put_backup``/``upload_multipart``.
    """

    key: str
    upload_id: str
    initiated: dt.datetime


class MultipartUploadsUnsupportedError(Exception):
    """The store does not support ListMultipartUploads (issue #903).

    Raised by :meth:`S3Client.list_multipart_uploads` when the backend rejects the
    operation (e.g. a SeaweedFS build without it). The sweep catches this, logs a
    WARN advising the ``AbortIncompleteMultipartUpload`` bucket lifecycle rule, and
    continues — orphan-part hygiene degrades to the lifecycle rule rather than
    failing the whole sweep.
    """


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

    async def list_multipart_uploads(self, prefix: str) -> list[S3MultipartUpload]:
        """List in-progress multipart uploads whose key starts with ``prefix``.

        Raises :class:`MultipartUploadsUnsupportedError` if the backend rejects the
        operation (issue #903).
        """
        ...

    async def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        """Abort one in-progress multipart upload, discarding its parts. Idempotent."""
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
        # Per-server publish/edit serialization (issue #899). Every authoritative
        # mutation of ``current/`` and its generation marker — a snapshot commit, a
        # backup restore, and each in-place file edit — takes the server's lock for
        # the mutate+bump critical section, closing the upload-window clobber the
        # pre-stream data-plane guard cannot: ``commit_snapshot`` re-reads the
        # generation and bumps under the same lock an edit takes, so an edit cannot
        # land between the commit's stale re-check and its pointer flip. The adapter
        # is async on one event loop, so an ``asyncio.Lock`` (not a thread lock) is
        # the right primitive. Locks are created lazily and never reclaimed (one per
        # server is negligible; reclaim would re-introduce the race the lock closes).
        # IN-PROCESS ONLY: this serializes within ONE uvicorn process (today's
        # single-process deployment, consistent with the in-process staging leases). A
        # future multi-process / multi-replica deployment would need a shared lock
        # (e.g. a conditional-write/compare-and-set on the pointer) or it silently
        # reopens this race.
        self._server_locks: dict[str, asyncio.Lock] = {}

    def _server_lock(
        self, community_id: CommunityId, server_id: ServerId
    ) -> asyncio.Lock:
        prefix = self._server_prefix(community_id, server_id)
        lock = self._server_locks.get(prefix)
        if lock is None:
            lock = asyncio.Lock()
            self._server_locks[prefix] = lock
        return lock

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

    async def _read_marker(
        self, client: S3Client, server_prefix: str
    ) -> tuple[int, str | None]:
        """Return the (generation, publisher) marker, or (0, None) when absent.

        Line 1 is the generation; line 2 (optional) is the publishing Worker id.
        An unreadable body (never published, an older single-line marker, or a
        corrupt value) falls back to a generation of 0 / no publisher.
        """

        key = server_prefix + _GENERATION
        if await client.head_object(key) is None:
            return 0, None
        raw = (await _read_all(client, key)).decode()
        lines = raw.splitlines()
        try:
            generation = int(lines[0]) if lines else 0
        except ValueError:
            return 0, None
        publisher = lines[1].strip() if len(lines) > 1 else ""
        return generation, (publisher or None)

    async def _read_generation(self, client: S3Client, server_prefix: str) -> int:
        """Return the working-set generation marker, or 0 when absent/unreadable."""

        generation, _ = await self._read_marker(client, server_prefix)
        return generation

    async def _read_publisher(self, client: S3Client, server_prefix: str) -> str | None:
        """Return the Worker id recorded for the latest publish (issue #847).

        Absent (never published, an older Worker, or a None publisher) -> no claim,
        so the guard cannot prove a foreign publisher and stays permissive.
        """

        _, publisher = await self._read_marker(client, server_prefix)
        return publisher

    async def _bump_generation(
        self, client: S3Client, server_prefix: str, publisher: str | None
    ) -> int:
        """Bump the generation and record the publisher in ONE marker object.

        A read-modify-write of the single combined marker (issues #763, #847): line 1
        the bumped generation, line 2 the publishing Worker id (omitted when None).
        Writing both as one atomic PUT keeps the pair consistent — a crash between two
        separate writes could attribute the previous publisher to the new generation
        and invert the publish-time guard. Snapshots for one server are serialized by
        the scheduler (one in-flight transfer), so the marker is not contended,
        mirroring the single-pointer flip just above which is likewise not
        CAS-protected.
        """

        new_generation = await self._read_generation(client, server_prefix) + 1
        body = (
            str(new_generation)
            if publisher is None
            else f"{new_generation}\n{publisher}"
        )
        await client.put_object(server_prefix + _GENERATION, body.encode())
        return new_generation

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

        Orphan multipart uploads (issue #903): a hard crash mid-``put_backup`` (or
        mid-snapshot-member upload) leaves in-progress multipart upload parts that
        never complete and are never listed as objects, so the prefix sweep above
        cannot see them. They are reclaimed separately by ``_sweep_multipart`` via
        ListMultipartUploads + AbortMultipartUpload, with an age threshold so a live
        upload is never aborted. If the backend does not support ListMultipartUploads
        the sweep degrades to a WARN advising the ``AbortIncompleteMultipartUpload``
        bucket lifecycle rule and continues the rest of the sweep.
        """

        async with self._client_factory() as client:
            objs = await client.list_objects("communities/")
            for server_prefix, keys in _group_by_server(objs).items():
                await self._sweep_server(client, server_prefix, keys)
            await self._sweep_multipart(client)

    async def _sweep_multipart(self, client: S3Client) -> None:
        """Abort orphan in-progress multipart uploads older than the age threshold.

        A crash mid-``put_backup`` (or mid-snapshot-member upload) leaves multipart
        parts that the object-listing sweep cannot see (issue #903). List the
        in-progress uploads under this adapter's key prefixes — ``communities/``
        (snapshot members, backups) AND ``jars/`` (``put_jar`` uploads via multipart
        too, issue #916) — and abort only those initiated more than
        ``_MULTIPART_SWEEP_MIN_AGE_S`` ago, so a live upload is never aborted. A
        backend that does not support ListMultipartUploads degrades to a WARN
        advising the ``AbortIncompleteMultipartUpload`` bucket lifecycle rule,
        leaving the rest of the sweep intact.
        """

        try:
            uploads = [
                upload
                for prefix in ("communities/", "jars/")
                for upload in await client.list_multipart_uploads(prefix)
            ]
        except MultipartUploadsUnsupportedError:
            _LOG.warning(
                "object store does not support ListMultipartUploads; orphan "
                "multipart upload parts will not be aborted by the sweep — configure "
                "an AbortIncompleteMultipartUpload bucket lifecycle rule instead "
                "(issue #903)"
            )
            return
        now = dt.datetime.now(dt.UTC)
        for upload in uploads:
            age = (now - upload.initiated).total_seconds()
            if age >= _MULTIPART_SWEEP_MIN_AGE_S:
                await client.abort_multipart_upload(upload.key, upload.upload_id)

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

    async def commit_snapshot(
        self,
        handle: SnapshotHandle,
        *,
        publisher: str | None = None,
        expected_base: int | None = None,
    ) -> int:
        h = _as_object_handle(handle)
        if h.consumed:
            raise SnapshotHandleError("snapshot handle already committed or aborted")
        incoming = self._incoming_prefix(h.community_id, h.server_id, h.transfer_id)
        server_prefix = self._server_prefix(h.community_id, h.server_id)
        async with self._client_factory() as client:
            staged = await client.list_objects(incoming)
            if not staged:
                # The "proven complete" gate (Section 4.1): an empty staging area is
                # not a publishable transfer. The end-of-stream + manifest/size
                # integrity check is part of the data-plane contract (epic #8); this
                # is the gate that will host it.
                raise IncompleteTransferError("no staged objects to publish")
            # Content-integrity gate (issue #750): walk the staged ``.mca`` region
            # bodies for structural corruption BEFORE the pointer flip, mirroring the
            # fs adapter's create gate (#749). On corruption, clean the staging prefix
            # (mirroring abort) and raise; the pointer is never flipped, so the prior
            # snapshot is retained (last-known-good, #703). The object backend has no
            # local working-set tree, so each region is checked from its object body
            # one at a time (bounded per-member, like the backup builder). The single
            # region rule set (issue #927): a 26.x world's unpadded tail is not a tear,
            # on any source — the byte-precise check still catches realistic tears.
            report = await self._check_staged_regions(client, incoming, staged)
            if not report.healthy:
                await _delete_prefix(client, incoming)
                self._release_staging(incoming)
                h.consumed = True
                raise IntegrityCheckError(report)
            # Materialize the staged objects into a fresh snapshot prefix BEFORE the
            # lock (issue #920): the copy is the publish's long pole (minutes for a
            # big world) and touches nothing live, so it must not hold the per-server
            # lock and block every in-place edit for that long.
            new_prefix = await self._copy_into_snapshot(
                client, h.community_id, h.server_id, incoming, staged
            )
            # Take the server lock (issue #899) for the stale re-check, the
            # missing-region prior read, the pointer flip, and the generation bump —
            # the same lock an in-place edit takes for its mutate+bump. Holding it
            # across these steps closes the upload-window clobber: an at-rest edit or a
            # backup restore cannot land between the commit's re-check and its pointer
            # flip. The bulk copy ran before the lock and the old-prefix GC runs after
            # it; only the atomic flip + bump (+ the gates that must be atomic with the
            # flip) are inside.
            async with self._server_lock(h.community_id, h.server_id):
                if expected_base is not None:
                    current = await self._read_generation(client, server_prefix)
                    if current > expected_base:
                        # The store advanced past the guard's base during the upload
                        # window: an at-rest edit or restore landed after the
                        # pre-stream guard passed. Discard the staging AND the copied
                        # (never-pointed-at) snapshot prefix exactly as the other
                        # refusal paths do (the prior ``current`` keeps the newer
                        # copy, no bump) and raise so the edge maps it to 409
                        # stale_generation; the Worker re-bases on next start.
                        await _delete_prefix(client, new_prefix)
                        await _delete_prefix(client, incoming)
                        self._release_staging(incoming)
                        h.consumed = True
                        raise StaleGenerationError(expected_base, current)
                # Missing-region gate (issue #854): the structural check above only
                # sees objects that EXIST — a vanished region object would publish
                # silently and MC would regenerate the chunks. Compare the staged
                # region-object names against the prior snapshot's per region-bearing
                # directory and refuse when a dimension that still has regions lost
                # SOME of them (partial loss). A full-dimension delete (all regions
                # gone) is allowed; first publish (no pointer) has no prior set, so
                # nothing is flagged. Name-only — no bodies.
                prior_prefix = await self._read_pointer(client, server_prefix)
                if prior_prefix is not None:
                    prior_objs = await client.list_objects(prior_prefix)
                    missing = compare_region_name_sets(
                        _region_names_by_dir(incoming, staged),
                        _region_names_by_dir(prior_prefix, prior_objs),
                    )
                    if not missing.complete:
                        await _delete_prefix(client, new_prefix)
                        await _delete_prefix(client, incoming)
                        self._release_staging(incoming)
                        h.consumed = True
                        raise MissingRegionsError(missing)
                old_prefix = await self._flip_pointer(
                    client, h.community_id, h.server_id, new_prefix
                )
                # Bump the working-set generation now that the pointer references the
                # new snapshot (issue #763) and record the publishing Worker (issue
                # #847) in ONE atomic marker. Every authoritative publish that
                # replaces ``current/`` bumps it — a snapshot commit here and a backup
                # restore (issue #873) — so a same-worker scratch never wrongly skips
                # the post-publish hydrate.
                generation = await self._bump_generation(
                    client, server_prefix, publisher
                )
            # Reclaim the staging prefix and the superseded snapshot prefix AFTER the
            # lock (issue #920): no edit can observe the old prefix once the flip is
            # published (edits re-read the pointer under the lock), so this is safe
            # outside the lock. It MUST stay INSIDE the client context, though, or the
            # GC runs on a closed client and the aioboto3 backend lazily opens a fresh
            # aiohttp ClientSession/connector that nothing closes — one leaked per
            # publish (issue #948).
            await self._gc_after_flip(client, incoming, old_prefix, new_prefix)
        # Release the staging lease so a later sweep is not blocked by a now-dead
        # handle (issue #160).
        self._release_staging(incoming)
        h.consumed = True
        return generation

    async def abort_snapshot(self, handle: SnapshotHandle) -> None:
        h = _as_object_handle(handle)
        incoming = self._incoming_prefix(h.community_id, h.server_id, h.transfer_id)
        async with self._client_factory() as client:
            await _delete_prefix(client, incoming)
        self._release_staging(incoming)
        h.consumed = True

    async def current_generation(
        self, community_id: CommunityId, server_id: ServerId
    ) -> int:
        # Read the counter ``commit_snapshot`` bumps (issue #763). No marker (never
        # published) -> generation 0.
        server_prefix = self._server_prefix(community_id, server_id)
        async with self._client_factory() as client:
            return await self._read_generation(client, server_prefix)

    async def current_publisher(
        self, community_id: CommunityId, server_id: ServerId
    ) -> str | None:
        # Read the Worker id recorded for the latest publish (issue #847 bug 3).
        server_prefix = self._server_prefix(community_id, server_id)
        async with self._client_factory() as client:
            return await self._read_publisher(client, server_prefix)

    async def check_current_health(
        self, community_id: CommunityId, server_id: ServerId
    ) -> WorkingSetReport:
        # The one-shot sweep's snapshot fsck (issue #744) is fs-only: it walks a
        # local working-set directory (issue #738), which the object backend does
        # not materialize. The authoritative-create/restore gates ARE wired on this
        # adapter (#750), but the read-only sweep fsck stays fs-only for now, so a
        # healthy report is returned to satisfy the Port. ``del`` the scope to mark
        # it intentionally unused.
        del community_id, server_id
        return WorkingSetReport()

    async def prune_to_final_snapshot(
        self, community_id: CommunityId, server_id: ServerId
    ) -> None:
        # The DeleteServer reclaim path (issue #777): pack the live snapshot into a
        # single retained ``final.tar.gz`` object at the server prefix, then drop the
        # working-set objects (snapshots/, incoming/, versions/) plus the pointer and
        # generation markers. ``backups/`` is left untouched — the caller prunes
        # archives through its own seam.
        server_prefix = self._server_prefix(community_id, server_id)
        async with self._client_factory() as client:
            snapshot_prefix = await self._read_pointer(client, server_prefix)
            if snapshot_prefix is not None:
                objs = sorted(
                    await client.list_objects(snapshot_prefix), key=lambda o: o.key
                )
                # Build the self-contained tar.gz to local scratch (gzip streams, so
                # the bodies are not all held at once), then upload it as one object.
                # The upload makes ``final.tar.gz`` appear atomically BEFORE any
                # working-set object is deleted, so a pack/upload failure leaves the
                # working set intact and the error propagates (fail-closed, #777).
                fd, spool_name = await asyncio.to_thread(
                    tempfile.mkstemp, prefix=".final.", suffix=".tar.gz"
                )
                await asyncio.to_thread(_close_fd, fd)
                spool = Path(spool_name)
                try:
                    await _write_backup_targz(client, snapshot_prefix, objs, spool)
                    await client.upload_multipart(
                        server_prefix + "final.tar.gz", _file_parts(spool)
                    )
                finally:
                    await asyncio.to_thread(spool.unlink, missing_ok=True)
                # Invalidate the pointer FIRST, the instant final.tar.gz is durable:
                # it is the one marker that says the working set is still live and
                # re-packable. A crash after this point leaves no pointer, so a
                # retried delete reads ``snapshot_prefix is None`` and takes the GC-
                # only branch below — it never re-lists a half-deleted prefix and
                # overwrites the good final.tar.gz with an empty/partial pack (#777).
                await client.delete_object(server_prefix + _POINTER)
            # The final archive is durable and the pointer is gone (or nothing was
            # published); reclaim the remaining working-set objects and the generation
            # marker. Idempotent: a retry that found no pointer arrives here directly
            # and completes the GC without re-packing.
            await _delete_prefix(client, server_prefix + "snapshots/")
            await _delete_prefix(client, server_prefix + "incoming/")
            await _delete_prefix(client, server_prefix + "versions/")
            await client.delete_object(server_prefix + _GENERATION)

    async def _check_staged_regions(
        self,
        client: S3Client,
        staged_prefix: str,
        staged: list[S3Object],
    ) -> WorkingSetReport:
        """Structurally fsck the staged ``.mca`` region objects (issue #750).

        The object backend has no local working-set tree for
        :func:`check_working_set` to walk, so each ``.mca`` object is fetched and
        checked from its body one at a time — bounded per-member, the same memory
        posture as the backup builder, never the whole working set at once. Returns
        a :class:`WorkingSetReport` whose ``corrupt`` list names the staged member
        of each structurally torn region (paths relative to ``staged_prefix``). The
        single region rule set (issue #927); see :func:`check_region_bytes`.
        """

        scanned = 0
        corrupt: list[RegionFinding] = []
        for obj in staged:
            name = obj.key[len(staged_prefix) :]
            if not name.endswith(".mca"):
                continue
            scanned += 1
            data = await _read_all(client, obj.key)
            finding = check_region_bytes(name, data)
            if finding is not None:
                corrupt.append(finding)
        return WorkingSetReport(scanned=scanned, corrupt=corrupt)

    async def _copy_into_snapshot(
        self,
        client: S3Client,
        community_id: CommunityId,
        server_id: ServerId,
        staged_prefix: str,
        staged: list[S3Object],
    ) -> str:
        """Materialize the staged objects under a fresh ``snapshots/<id>/`` prefix.

        The bulk copy (minutes for a big world) touches NOTHING live -- it only
        writes a new, not-yet-pointed-at prefix -- so it runs BEFORE the per-server
        lock (issue #920) to keep the lock's critical section short. Returns the new
        snapshot prefix for :meth:`_flip_pointer`.
        """

        self._seam.reach(PublishPhase.AFTER_STAGE)

        snapshot_id = SnapshotId.new()
        new_prefix = self._snapshot_prefix(community_id, server_id, snapshot_id.value)
        for obj in staged:
            rest = obj.key[len(staged_prefix) :]
            await client.copy_object(obj.key, new_prefix + rest)

        self._seam.reach(PublishPhase.AFTER_MOVE)
        return new_prefix

    async def _flip_pointer(
        self,
        client: S3Client,
        community_id: CommunityId,
        server_id: ServerId,
        new_prefix: str,
    ) -> str | None:
        """Atomically flip the pointer onto ``new_prefix`` (the publish's atomic step).

        MUST run under the per-server lock (issue #920) so the flip is atomic with
        the commit's stale re-check and so an in-place edit -- which now re-reads the
        pointer under the SAME lock -- observes either the pre- or post-flip prefix,
        never a torn state. Returns the superseded prefix for the post-lock GC.
        """

        server_prefix = self._server_prefix(community_id, server_id)
        old_prefix = await self._read_pointer(client, server_prefix)
        # The single atomic step: overwrite the one pointer object. After it returns
        # the pointer references either the old or the new prefix, never a partial.
        await client.put_object(
            server_prefix + _POINTER,
            json.dumps({"snapshot": new_prefix}).encode(),
        )

        self._seam.reach(PublishPhase.AFTER_FLIP)
        return old_prefix

    async def _gc_after_flip(
        self,
        client: S3Client,
        staged_prefix: str,
        old_prefix: str | None,
        new_prefix: str,
    ) -> None:
        """Reclaim the staging prefix and the superseded snapshot prefix.

        Runs AFTER releasing the per-server lock (issue #920) to keep the lock's
        critical section short. This is safe against the bug-1 fix: every in-place
        edit now re-reads the pointer UNDER the lock, after the flip, so once the
        flip is published no edit can still observe ``old_prefix`` -- the only
        readers of ``old_prefix`` left are active hydrate streams, which hold a lease
        and are skipped here exactly as before (Section 4.2/4.3). A leased old prefix
        is left for the next sweep to reclaim once the reader releases.
        """

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
            # Content-integrity gate (issue #750): never archive a known-corrupt
            # world, mirroring the fs adapter (#739). Walk the live snapshot's
            # ``.mca`` region bodies BEFORE writing the archive; any corrupt region
            # refuses the backup and no ``.tar.gz`` object is uploaded (fail-closed,
            # #703). The single region rule set (issue #927): a snapshot may hold a
            # legitimate unpadded set (gated when it published), so the at-rest backup
            # gate tolerates the unpadded tail, mirroring the fs adapter.
            report = await self._check_staged_regions(client, snapshot_prefix, objs)
            if not report.healthy:
                raise IntegrityCheckError(report)
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
                    # Restore-direction integrity gate (issue #750), mirroring the fs
                    # adapter (#743): walk the staged ``.mca`` region bodies for
                    # structural corruption BEFORE publishing into the live snapshot.
                    # By default (``force=False``) a corrupt restore is refused — the
                    # staging is cleaned (the except below) and IntegrityCheckError is
                    # raised, so the prior snapshot is left untouched (last-known-good,
                    # #703). With ``force=True`` the operator override publishes anyway
                    # (better a deliberate corrupt restore than no restore, #703). The
                    # report is returned either way so the use case can quarantine +
                    # audit a forced corrupt restore. The single region rule set (issue
                    # #927): a backup created from an unpadded snapshot is itself
                    # live-format, so the restore gate tolerates the unpadded tail,
                    # mirroring the fs adapter.
                    report = await self._check_staged_regions(client, incoming, staged)
                    if not report.healthy and not force:
                        raise IntegrityCheckError(report)
                    # Materialize the staged objects into a fresh snapshot prefix
                    # BEFORE the lock (issue #920): the copy touches nothing live, so
                    # it must not hold the per-server lock and block edits for its
                    # duration.
                    new_prefix = await self._copy_into_snapshot(
                        client, community_id, server_id, incoming, staged
                    )
                    # Flip + bump under the server lock (issue #899), serializing with
                    # a concurrent snapshot commit's generation re-check so the restore
                    # cannot interleave with the commit's pointer flip.
                    async with self._server_lock(community_id, server_id):
                        old_prefix = await self._flip_pointer(
                            client, community_id, server_id, new_prefix
                        )
                        # Bump the generation on this authoritative publish (issue
                        # #873), mirroring the fs adapter: a restore replaces
                        # ``current/`` like a snapshot commit, so it MUST advance the
                        # store generation or a same-worker scratch with held == store
                        # skips the hydrate (#767) on the next start and boots the
                        # PRE-restore world. The sentinel publisher (RESTORE_PUBLISHER)
                        # makes the publish-time guard refuse an in-flight stale
                        # snapshot from a real Worker (different publisher), closing the
                        # restore-clobber window (#873).
                        server_prefix = self._server_prefix(community_id, server_id)
                        await self._bump_generation(
                            client, server_prefix, RESTORE_PUBLISHER
                        )
                    # Reclaim the staging + superseded prefixes after the lock (#920).
                    await self._gc_after_flip(client, incoming, old_prefix, new_prefix)
                    return report
                except BaseException:
                    await _delete_prefix(client, incoming)
                    raise
                finally:
                    await asyncio.to_thread(spool.unlink, missing_ok=True)
        finally:
            self._release_staging(incoming)

    async def check_backup_health(
        self, community_id: CommunityId, server_id: ServerId, key: BackupKey
    ) -> WorkingSetReport:
        # The one-shot sweep's per-backup fsck (issue #744) extracts a local working
        # set and walks it (issue #738), which the object backend does not stage —
        # so this read-only sweep fsck stays fs-only for now even though the
        # authoritative-create/restore gates ARE wired on this adapter (#750). The
        # object's existence is still confirmed (so an unknown key is a
        # NotFoundError, matching the fs adapter) before the healthy report is
        # returned to satisfy the Port.
        backup_key = self._backup_key(community_id, server_id, key)
        async with self._client_factory() as client:
            if await client.head_object(backup_key) is None:
                raise NotFoundError(f"backup not found: {key.value}")
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
            # Read the live pointer and run every precondition that depends on it
            # INSIDE the server lock (issue #899/#920): the pointed-at prefix is a
            # concurrent commit's flip + GC target, so reading it outside the lock
            # would let the commit flip the pointer and delete the old prefix, after
            # which this edit would PUT under the GC'd prefix and flip the pointer
            # BACK to it -- total world loss. This is the same lock the commit's
            # re-check + flip + bump takes, so an edit cannot interleave with it.
            async with self._server_lock(community_id, server_id):
                snapshot_prefix = await self._read_pointer(client, server_prefix)
                if snapshot_prefix is None:
                    # A never-snapshotted server has no live prefix to edit in place
                    # (issue #205). Initialize the first published version containing
                    # just this file through the same pointer-flip publish path a
                    # snapshot uses. The caller already holds the lock, so
                    # ``_publish_initial`` does NOT re-acquire it.
                    await self._publish_initial(
                        client, community_id, server_id, sub, data
                    )
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
                # stays named explicitly; the pointer PUT is the atomic point
                # (Section 4.4).
                await client.put_object(key, data)
                self._seam.reach(PublishPhase.AFTER_FILE_TEMP_WRITE)
                await client.put_object(
                    self._pointer_key(community_id, server_id),
                    json.dumps({"snapshot": snapshot_prefix}).encode(),
                )
                # Bump the generation on this authoritative edit (issue #889): an
                # in-place write replaces the published world just like a
                # snapshot/restore, so it advances the store generation and stamps the
                # API_EDIT_PUBLISHER sentinel — otherwise a same-worker scratch with
                # held == store skips the post-edit hydrate (#767) and its stale
                # snapshot clobbers the edit.
                await self._bump_generation(client, server_prefix, API_EDIT_PUBLISHER)

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
        through the copy/flip/GC phases — the same pointer-flip path a snapshot
        commit uses. The staging prefix is pinned with an active-staging lease for
        the life of the operation so a concurrent sweep does not GC it (issue #160).

        The CALLER (``write_file``) already holds the per-server lock (issue #920)
        — it took the lock before the never-snapshotted pointer read that routes
        here — so this single-file publish runs entirely under that lock and does NOT
        re-acquire it (``asyncio.Lock`` is not reentrant).
        """

        incoming = self._incoming_prefix(community_id, server_id, uuid.uuid4().hex)
        self._register_staging(incoming)
        try:
            await client.put_object(incoming + sub, data)
            staged = await client.list_objects(incoming)
            new_prefix = await self._copy_into_snapshot(
                client, community_id, server_id, incoming, staged
            )
            old_prefix = await self._flip_pointer(
                client, community_id, server_id, new_prefix
            )
            # The first published version of a never-snapshotted server is still an
            # authoritative edit (issue #889): bump past generation 0 and stamp the
            # API_EDIT_PUBLISHER sentinel so the same staleness reasoning applies.
            await self._bump_generation(
                client,
                self._server_prefix(community_id, server_id),
                API_EDIT_PUBLISHER,
            )
            await self._gc_after_flip(client, incoming, old_prefix, new_prefix)
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
            # Read the live pointer and check the precondition INSIDE the server lock
            # (issue #899/#920): the pointed-at prefix is a concurrent commit's flip +
            # GC target, so reading it outside the lock would delete from -- and bump
            # over -- a prefix the commit then flips away and GCs. Same lock the
            # commit's re-check + flip + bump takes.
            async with self._server_lock(community_id, server_id):
                snapshot_prefix = await self._live_snapshot_prefix(
                    client, community_id, server_id
                )
                key = snapshot_prefix + sub
                if await client.head_object(key) is None:
                    raise NotFoundError(f"file not found: {rel_path.value}")
                # Capture the content BEFORE removing it (Section 5), so a delete is
                # reversible by rollback exactly like an overwrite is.
                await self._capture_version(
                    client, community_id, server_id, rel_path, key
                )
                self._seam.reach(PublishPhase.AFTER_VERSION_CAPTURE)
                await client.delete_object(key)
                # Re-write the pointer so the published state stays named explicitly,
                # mirroring write_file's post-mutation pointer rewrite (Section 4.4).
                await client.put_object(
                    self._pointer_key(community_id, server_id),
                    json.dumps({"snapshot": snapshot_prefix}).encode(),
                )
                # Authoritative edit -> bump the generation (issue #889).
                await self._bump_generation(
                    client,
                    self._server_prefix(community_id, server_id),
                    API_EDIT_PUBLISHER,
                )

    async def delete_dir(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        sub = self._safe_subkey(rel_path)
        dir_suffix = sub + "/" if sub else ""
        async with self._client_factory() as client:
            # No per-file version capture (Port contract): whole-subtree recovery
            # is the backups' job (Section 3.3). Delete every object under the dir
            # prefix, then re-write the pointer to keep the state named explicitly.
            # Read the live pointer and check the precondition INSIDE the server lock
            # (issue #899/#920): the pointed-at prefix is a concurrent commit's flip +
            # GC target, so reading it outside the lock would delete from -- and bump
            # over -- a prefix the commit then flips away and GCs. Same lock the
            # commit's re-check + flip + bump takes.
            async with self._server_lock(community_id, server_id):
                snapshot_prefix = await self._live_snapshot_prefix(
                    client, community_id, server_id
                )
                objs = await client.list_objects(snapshot_prefix + dir_suffix)
                if not objs:
                    raise NotFoundError(f"directory not found: {rel_path.value}")
                for obj in objs:
                    await client.delete_object(obj.key)
                await client.put_object(
                    self._pointer_key(community_id, server_id),
                    json.dumps({"snapshot": snapshot_prefix}).encode(),
                )
                # Authoritative edit -> bump the generation (issue #889).
                await self._bump_generation(
                    client,
                    self._server_prefix(community_id, server_id),
                    API_EDIT_PUBLISHER,
                )

    async def rename_dir(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        from_path: RelPath,
        to_path: RelPath,
    ) -> None:
        from_sub = self._safe_subkey(from_path)
        to_sub = self._safe_subkey(to_path)
        from_suffix = from_sub + "/" if from_sub else ""
        to_suffix = to_sub + "/" if to_sub else ""
        async with self._client_factory() as client:
            # No per-file version capture (Port contract): whole-subtree recovery
            # is the backups' job (Section 3.3). Copy every object to the new
            # prefix, delete the originals, then re-write the pointer.
            # Read the live pointer INSIDE the server lock (issue #899/#920).
            async with self._server_lock(community_id, server_id):
                snapshot_prefix = await self._live_snapshot_prefix(
                    client, community_id, server_id
                )
                objs = await client.list_objects(snapshot_prefix + from_suffix)
                if not objs:
                    raise NotFoundError(
                        f"directory not found: {from_path.value}"
                    )
                for obj in objs:
                    rest = obj.key[len(snapshot_prefix + from_suffix) :]
                    await client.copy_object(
                        obj.key, snapshot_prefix + to_suffix + rest
                    )
                for obj in objs:
                    await client.delete_object(obj.key)
                await client.put_object(
                    self._pointer_key(community_id, server_id),
                    json.dumps({"snapshot": snapshot_prefix}).encode(),
                )
                # Authoritative edit -> bump the generation (issue #889).
                await self._bump_generation(
                    client,
                    self._server_prefix(community_id, server_id),
                    API_EDIT_PUBLISHER,
                )

    async def make_dir(
        self, community_id: CommunityId, server_id: ServerId, rel_path: RelPath
    ) -> None:
        sub = self._safe_subkey(rel_path)
        # Write a zero-byte marker object so the empty directory is visible in
        # listings (issue #1125). The marker is filtered out by ``_entries_at_level``
        # so it never appears as a file entry.
        async with self._client_factory() as client:
            server_prefix = self._server_prefix(community_id, server_id)
            async with self._server_lock(community_id, server_id):
                snapshot_prefix = await self._live_snapshot_prefix(
                    client, community_id, server_id
                )
                marker_key = snapshot_prefix + sub + "/" + _DIR_MARKER
                await client.put_object(marker_key, b"")
                await self._bump_generation(client, server_prefix, API_EDIT_PUBLISHER)

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


def _region_names_by_dir(prefix: str, objs: list[S3Object]) -> dict[Path, set[str]]:
    """Group the ``.mca`` objects under ``prefix`` by their region-bearing directory.

    Mirrors the fs adapter's :func:`_region_sets_by_dir` for the object backend
    (issue #854): each ``*.mca`` key relative to ``prefix`` is bucketed by its
    parent path, yielding the same dir -> name-set shape
    :func:`compare_region_name_sets` diffs. Non-``.mca`` keys are ignored.
    """
    by_dir: dict[Path, set[str]] = {}
    for obj in objs:
        if not obj.key.endswith(".mca"):
            continue
        rel = PurePosixPath(obj.key[len(prefix) :])
        by_dir.setdefault(Path(rel.parent), set()).add(rel.name)
    return by_dir


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
        if rest == _DIR_MARKER:
            # Marker placed by make_dir (issue #1125) — hide it from listings.
            continue
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
