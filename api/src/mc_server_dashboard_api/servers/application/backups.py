"""Backup-management use cases with state branching (Section 6.9, 6.11).

These run after the route's authorization dependency admitted the caller, so they
assume an authorized member and only do the backup work. Create branches on server
state per the 6.9 table:

- **at rest** (``server.is_at_rest()``: desired=stopped, observed in
  {stopped, unknown}) -> archive directly from the authoritative Storage copy,
  through the :class:`BackupArchiveStore` seam (FR-BAK-2).
- **running** (desired=running, observed=running, a worker assigned) ->
  ``save-all`` via the RCON command seam, then an on-demand snapshot (the
  :class:`SnapshotServer` hook, PR #114) so the just-saved live working set is
  published to the authoritative copy, then archive that fresh snapshot. The
  archive always reads the authoritative copy; the running path only makes that
  copy current first (Section 6.9, FR-BAK-2).
- **anything else** (starting/stopping/restarting/crashed, or a desired/observed
  mismatch) -> :class:`BackupUnsettledError` (409): neither source is well-defined.

Each create records a :class:`Backup` metadata row (source manual or scheduled,
the archive ref, the actor, and the archive size when cheap) — the row is the
index into the Storage archive (DATABASE.md Section 8).

Restore requires the server **at rest** (FR-BAK-4): a hot replacement of a live
working set is unsafe, so a running server is 409. Restore republishes the
archive into the authoritative copy atomically; the restored state hydrates on the
next start with no extra work (hydrate reads ``current``).

Delete removes both the archive and the metadata row. Ordering: the **archive is
deleted first, the metadata row last** — so a crash between the two leaves a
metadata row whose archive is already gone (a dangling-pointer row, harmless and
fixable: delete is idempotent and re-running it removes the row), never a metadata
row deleted while its archive lingers as an unreferenced orphan with no row to find
it by. ``delete_backup`` on Storage is idempotent, so the archive delete is safe to
retry.
"""

from __future__ import annotations

import io
import tarfile
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import IO

from mc_server_dashboard_api.servers.application.command_dispatch import (
    dispatch_failure,
)
from mc_server_dashboard_api.servers.application.files import (
    MAX_ARCHIVE_ENTRIES,
    MAX_DECOMPRESSED_BYTES,
    MAX_UPLOAD_BYTES,
)
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    SnapshotServer,
)
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupId,
    BackupSource,
    BackupStatistics,
)
from mc_server_dashboard_api.servers.domain.backup_store import BackupArchiveStore
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.control_plane import (
    ControlPlane,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    BackupNotFoundError,
    BackupUnsettledError,
    FileTooLargeError,
    InvalidBackupArchiveError,
    ServerNotFoundError,
    ServerNotStoppedError,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
)

# The RCON line that flushes the live world to disk before a running-server
# snapshot, so the archived working set is consistent (Section 6.9, FR-BAK-2).
_SAVE_ALL_LINE = "save-all flush"

# How much to stream per chunk when handing the uploaded archive to Storage; one
# bounded block in flight, never the whole archive re-buffered.
_UPLOAD_STREAM_CHUNK = 1024 * 1024


async def _load(
    uow: UnitOfWork, community_id: CommunityId, server_id: ServerId
) -> Server:
    server = await uow.servers.get_by_id(server_id)
    if server is None or server.community_id != community_id:
        raise ServerNotFoundError(str(server_id.value))
    return server


def _is_running(server: Server) -> bool:
    return (
        server.desired_state is DesiredState.RUNNING
        and server.observed_state is ObservedState.RUNNING
        and server.assigned_worker_id is not None
    )


@dataclass(frozen=True)
class CreateBackup:
    """Create a backup, branching at-rest -> Storage / running -> save-all+snapshot.

    ``backup:create`` (manual) and the scheduled path both run through here; the
    ``source`` and ``created_by`` differ per caller.
    """

    uow: UnitOfWork
    control_plane: ControlPlane
    backup_store: BackupArchiveStore
    snapshot_server: SnapshotServer
    clock: Clock

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        source: BackupSource,
        created_by: uuid.UUID | None = None,
    ) -> Backup:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)

        if _is_running(server):
            await self._save_all_and_snapshot(server)
        elif not server.is_at_rest():
            # starting / stopping / restarting / crashed / mismatch: no source.
            raise BackupUnsettledError(str(server_id.value))

        # Both the at-rest path and the running path (after the snapshot just
        # published) archive the authoritative copy (Section 6.9, FR-BAK-2).
        storage_ref = await self.backup_store.create_from_current(
            community_id=community_id, server_id=server_id
        )
        # Record the archive size now that it exists, so the row carries it (older
        # rows predate this and stay NULL — reported as unknown in stats, #281).
        size_bytes = await self.backup_store.size(
            community_id=community_id, server_id=server_id, storage_ref=storage_ref
        )
        backup = Backup(
            id=BackupId.new(),
            server_id=server_id,
            storage_ref=storage_ref,
            size_bytes=size_bytes,
            source=source,
            created_by=created_by,
            created_at=self.clock.now(),
        )
        async with self.uow:
            await self.uow.backups.add(backup)
            await self.uow.commit()
        return backup

    async def _save_all_and_snapshot(self, server: Server) -> None:
        """Flush the live world (save-all) then publish an on-demand snapshot.

        The running path of the 6.9 policy: the archive only ever reads the
        authoritative copy, so first make that copy current — ``save-all`` over
        RCON quiesces the world to the live working set, then the on-demand
        snapshot publishes it. A failed save-all or snapshot fails the create
        (the snapshot raises :class:`CommandDispatchError` / worker-unavailable).
        """

        assert server.assigned_worker_id is not None  # running invariant
        outcome = await self.control_plane.command(
            worker_id=server.assigned_worker_id,
            server_id=server.id,
            line=_SAVE_ALL_LINE,
        )
        if not outcome.success:
            raise dispatch_failure(server_id=server.id, kind="SaveAll", outcome=outcome)
        await self.snapshot_server(
            community_id=server.community_id, server_id=server.id
        )


@dataclass(frozen=True)
class ListBackups:
    """List a server's backups newest-first (backup:read).

    Community-scoped: a backup whose server is outside the path community is never
    returned (no cross-community signal, FR-COMM-3).
    """

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> list[Backup]:
        async with self.uow:
            await _load(self.uow, community_id, server_id)
            return await self.uow.backups.list_for_server(server_id)


@dataclass(frozen=True)
class RestoreBackup:
    """Restore a backup into the authoritative copy (backup:restore, FR-BAK-4).

    Requires the server at rest: a hot replacement of a live working set is unsafe
    (Section 6.9), so a running (or transitional) server is 409. The republish is
    atomic (Storage); the restored state hydrates on the next start.
    """

    uow: UnitOfWork
    backup_store: BackupArchiveStore

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        backup_id: BackupId,
    ) -> None:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
            backup = await self.uow.backups.get_by_id(backup_id)
            if backup is None or backup.server_id != server_id:
                raise BackupNotFoundError(str(backup_id.value))
            storage_ref = backup.storage_ref
        if not server.is_at_rest():
            raise ServerNotStoppedError(str(server_id.value))
        await self.backup_store.restore(
            community_id=community_id,
            server_id=server_id,
            storage_ref=storage_ref,
        )


@dataclass(frozen=True)
class DeleteBackup:
    """Delete a backup's archive then its metadata row (backup:delete).

    Archive first, metadata last (see module docstring): a crash between the two
    leaves a metadata row whose archive is gone (harmless, re-deletable), never an
    orphaned archive with no row to find it by. Storage delete is idempotent.
    """

    uow: UnitOfWork
    backup_store: BackupArchiveStore

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        backup_id: BackupId,
    ) -> None:
        async with self.uow:
            await _load(self.uow, community_id, server_id)
            backup = await self.uow.backups.get_by_id(backup_id)
            if backup is None or backup.server_id != server_id:
                raise BackupNotFoundError(str(backup_id.value))
            storage_ref = backup.storage_ref
        # Delete the archive first (idempotent), then the metadata row last.
        await self.backup_store.delete(
            community_id=community_id,
            server_id=server_id,
            storage_ref=storage_ref,
        )
        async with self.uow:
            await self.uow.backups.delete(backup_id)
            await self.uow.commit()


async def _load_backup(
    uow: UnitOfWork,
    community_id: CommunityId,
    server_id: ServerId,
    backup_id: BackupId,
) -> Backup:
    """Load a community-scoped backup, 404ing an unknown or cross-server id."""

    async with uow:
        await _load(uow, community_id, server_id)
        backup = await uow.backups.get_by_id(backup_id)
        if backup is None or backup.server_id != server_id:
            raise BackupNotFoundError(str(backup_id.value))
        return backup


@dataclass(frozen=True)
class DownloadBackup:
    """Stream a backup archive in its native format (backup:read, issue #281).

    Resolves the (community-scoped) backup, then opens a read stream over the
    stored archive bytes through the :class:`BackupArchiveStore` seam — no
    recompression, the exact stored ``tar.gz`` bytes. An unknown / cross-server
    backup is :class:`BackupNotFoundError` (the edge 404s, no existence signal).
    """

    uow: UnitOfWork
    backup_store: BackupArchiveStore

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        backup_id: BackupId,
    ) -> AsyncIterator[bytes]:
        backup = await _load_backup(self.uow, community_id, server_id, backup_id)
        return self.backup_store.open(
            community_id=community_id,
            server_id=server_id,
            storage_ref=backup.storage_ref,
        )


@dataclass(frozen=True)
class UploadBackup:
    """Store an off-host backup archive as a new restorable backup (issue #281).

    The archive is VALIDATED before it is stored: bounded by ``max_bytes``
    (:class:`FileTooLargeError` -> 413) and proven to open as a gzip tar whose
    every member is a traversal-safe relative path
    (:class:`InvalidBackupArchiveError` -> 422). Only then is it streamed into
    Storage verbatim (no recompression), its size recorded, and a ``source=uploaded``
    metadata row committed — so an uploaded backup is restorable through the exact
    same restore flow as a created one.

    The caps are fields so a test can inject a tiny cap and trip the guard with a
    small archive; production wiring uses the defaults.
    """

    uow: UnitOfWork
    backup_store: BackupArchiveStore
    clock: Clock
    max_bytes: int = MAX_UPLOAD_BYTES
    max_entries: int = MAX_ARCHIVE_ENTRIES
    max_decompressed_bytes: int = MAX_DECOMPRESSED_BYTES

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        content: bytes,
        created_by: uuid.UUID | None = None,
    ) -> Backup:
        async with self.uow:
            await _load(self.uow, community_id, server_id)

        if len(content) > self.max_bytes:
            raise FileTooLargeError(str(len(content)))
        _validate_backup_archive(
            content,
            max_entries=self.max_entries,
            max_decompressed_bytes=self.max_decompressed_bytes,
        )

        storage_ref = await self.backup_store.store(
            community_id=community_id,
            server_id=server_id,
            stream=_chunked(content),
        )
        backup = Backup(
            id=BackupId.new(),
            server_id=server_id,
            storage_ref=storage_ref,
            size_bytes=len(content),
            source=BackupSource.UPLOADED,
            created_by=created_by,
            created_at=self.clock.now(),
        )
        async with self.uow:
            await self.uow.backups.add(backup)
            await self.uow.commit()
        return backup


@dataclass(frozen=True)
class ServerBackupStatistics:
    """Per-server backup usage: count, bytes, newest/oldest (backup:read, #281).

    Community-scoped (loads the community-checked server first). ``total_bytes``
    sums only the rows whose ``size_bytes`` is recorded; legacy NULL-size rows are
    counted in ``unknown_size_count`` and excluded from the total — an honest
    "unknown" rather than a wrong sum.
    """

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> BackupStatistics:
        async with self.uow:
            await _load(self.uow, community_id, server_id)
            rows = await self.uow.backups.list_for_server(server_id)
        return _aggregate(rows)


@dataclass(frozen=True)
class GlobalBackupStatistics:
    """Platform-wide backup usage (platform-admin, issue #281).

    The smallest honest global shape: total count, summed known bytes, the
    NULL-size (legacy/unknown) count, and the newest/oldest timestamps across the
    whole platform. Gated by the platform-admin axis at the edge, so no community
    scope applies.
    """

    uow: UnitOfWork

    async def __call__(self) -> BackupStatistics:
        async with self.uow:
            return await self.uow.backups.global_statistics()


def _aggregate(rows: list[Backup]) -> BackupStatistics:
    known = [b.size_bytes for b in rows if b.size_bytes is not None]
    times = [b.created_at for b in rows]
    return BackupStatistics(
        count=len(rows),
        total_bytes=sum(known),
        unknown_size_count=len(rows) - len(known),
        newest=max(times) if times else None,
        oldest=min(times) if times else None,
    )


async def _chunked(content: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(content), _UPLOAD_STREAM_CHUNK):
        yield content[start : start + _UPLOAD_STREAM_CHUNK]


def _validate_backup_archive(
    content: bytes,
    *,
    max_entries: int,
    max_decompressed_bytes: int = MAX_DECOMPRESSED_BYTES,
) -> None:
    """Prove the upload is a traversal-safe, bounded gzip tar before storing (#281).

    Backups are self-contained ``tar.gz`` archives (STORAGE.md Section 2). The
    archive must open as a gzip tar, carry at most ``max_entries`` members, and
    every member must be a regular file or directory with a relative, non-escaping
    name (no absolute paths, no ``..``, no devices / symlink / hardlink members) —
    the same traversal discipline Storage's extraction enforces, applied here so a
    hostile archive is refused BEFORE it lands in the store.

    The compressed body is already capped, but a gzip member can expand ~1000x, so
    each file member's body is drained and the cumulative DECOMPRESSED byte count
    is bounded by ``max_decompressed_bytes`` (#287). The count is over actual bytes
    read, not the (forgeable) member header, so a member that under-reports its size
    cannot slip past. Raises :class:`InvalidBackupArchiveError` (422) on any
    violation.
    """

    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
            count = 0
            total = 0
            for member in tar:
                count += 1
                if count > max_entries:
                    raise InvalidBackupArchiveError("too many entries")
                if not (member.isfile() or member.isdir()):
                    raise InvalidBackupArchiveError(
                        f"unsafe member type: {member.name!r}"
                    )
                if _is_unsafe_member_name(member.name):
                    raise InvalidBackupArchiveError(
                        f"unsafe member path: {member.name!r}"
                    )
                if member.isfile():
                    handle = tar.extractfile(member)
                    if handle is not None:
                        total = _drain_capped(handle, total, max_decompressed_bytes)
    except tarfile.TarError as exc:
        raise InvalidBackupArchiveError("not a gzip tar archive") from exc


def _drain_capped(handle: IO[bytes], total: int, max_bytes: int) -> int:
    """Drain a member's decompressed body in chunks, bounding the cumulative total.

    Mirrors ``files._read_capped`` (#262) but counts only — the validator never
    needs the bytes, just proof the archive stays under the cap. ``total`` is the
    running sum across prior members; the sum is checked after every chunk so a
    single high-ratio member aborts mid-decompression rather than materializing
    first (the gzip-bomb defence). Returns the updated running total.
    """

    while True:
        chunk = handle.read(_UPLOAD_STREAM_CHUNK)
        if not chunk:
            return total
        total += len(chunk)
        if total > max_bytes:
            raise InvalidBackupArchiveError("decompressed size exceeds cap")


def _is_unsafe_member_name(name: str) -> bool:
    """True if a tar member name is absolute or escapes the extraction root."""

    if name.startswith("/"):
        return True
    parts = name.replace("\\", "/").split("/")
    return ".." in parts
