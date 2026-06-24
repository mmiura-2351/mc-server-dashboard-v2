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

import datetime as dt
import hashlib
import io
import logging
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
from mc_server_dashboard_api.servers.application.plugin_cache import (
    ingest_into_cache,
)
from mc_server_dashboard_api.servers.application.plugin_manifest import (
    parse_manifest_at_ingest,
)
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    SnapshotServer,
)
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
    BackupStatistics,
)
from mc_server_dashboard_api.servers.domain.backup_author_directory import (
    BackupAuthorDirectory,
)
from mc_server_dashboard_api.servers.domain.backup_store import BackupArchiveStore
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.control_plane import (
    ControlPlane,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    BackupCorruptError,
    BackupNotFoundError,
    BackupUnsettledError,
    FileTooLargeError,
    InvalidBackupArchiveError,
    ServerNotFoundError,
    ServerNotStoppedError,
    UnsupportedPluginServerTypeError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileStore
from mc_server_dashboard_api.servers.domain.lifecycle_lock import (
    LifecycleLock,
    NullLifecycleLock,
)
from mc_server_dashboard_api.servers.domain.plugin import (
    PluginId,
    PluginSource,
    ServerPlugin,
    content_dir_for_server_type,
    loader_type_for_server_type,
    modrinth_loader_for_server_type,
)
from mc_server_dashboard_api.servers.domain.plugin_cache_store import PluginCacheStore
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
)

_LOG = logging.getLogger(__name__)

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
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        source: BackupSource,
        created_by: uuid.UUID | None = None,
    ) -> Backup:
        # Hold the per-server lifecycle lock across the at-rest check, the
        # (possibly running-path save-all + snapshot) source preparation, and the
        # archive of ``current`` (issue #827, #876): a lock-holding restore that
        # republishes ``current`` mid-tar would otherwise tear the archive (the
        # #827-class gap noted in #827). The same lock serializes this archive
        # against a concurrent restore/delete just as RestoreBackup serializes its
        # republish.
        async with self.lifecycle_lock.hold(server_id):
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
            # Record the archive size now that it exists, so the row carries it
            # (older rows predate this and stay NULL — reported as unknown in stats,
            # #281).
            size_bytes = await self.backup_store.size(
                community_id=community_id, server_id=server_id, storage_ref=storage_ref
            )
            backup = Backup(
                id=BackupId.new(),
                server_id=server_id,
                storage_ref=storage_ref,
                size_bytes=size_bytes,
                source=source,
                # Healthy by construction: ``create_from_current`` runs through the
                # integrity gate (#749), which refuses to archive a corrupt working
                # set — so reaching here means the archived contents were sound
                # (#742).
                health=BackupHealth.HEALTHY,
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


class _NullAuthorDirectory(BackupAuthorDirectory):
    """A directory that resolves no names (the default when none is wired)."""

    async def usernames_for(self, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        return {}


@dataclass(frozen=True)
class ListedBackup:
    """A backup with its author's display username resolved (issue #688).

    ``created_by_username`` is resolved through the :class:`BackupAuthorDirectory`
    seam. It is ``None`` when the backup has no actor (a scheduled backup) or when
    the author id no longer resolves (a deleted user) — the client then falls back
    to showing the raw id.
    """

    backup: Backup
    created_by_username: str | None


@dataclass(frozen=True)
class ListBackups:
    """List a server's backups newest-first (backup:read).

    Community-scoped: a backup whose server is outside the path community is never
    returned (no cross-community signal, FR-COMM-3).

    Resolves each backup's ``created_by`` to a display username through the
    :class:`BackupAuthorDirectory` seam (issue #688), in a single batch lookup over
    the page's distinct author ids — never one lookup per row.

    Lazily backfills legacy NULL ``size_bytes`` rows on read (issue #661): a row
    created before size tracking landed (#281) keeps ``size_bytes = NULL``, so the
    WebUI shows it as "unknown" and excludes it from the total. When such a row is
    listed and its archive still exists, the size is computed via the archive
    store and persisted, so it becomes a one-time per-row cost and the total
    becomes a full sum.
    """

    uow: UnitOfWork
    backup_store: BackupArchiveStore
    users: BackupAuthorDirectory = _NullAuthorDirectory()

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> list[ListedBackup]:
        async with self.uow:
            await _load(self.uow, community_id, server_id)
            rows = await self.uow.backups.list_for_server(server_id)
            rows = await _backfill_null_sizes(
                self.uow, self.backup_store, community_id, server_id, rows
            )
        author_ids = {row.created_by for row in rows if row.created_by is not None}
        usernames = await self.users.usernames_for(list(author_ids))
        return [
            ListedBackup(
                backup=row,
                created_by_username=(
                    usernames.get(row.created_by)
                    if row.created_by is not None
                    else None
                ),
            )
            for row in rows
        ]


@dataclass(frozen=True)
class RestoreResult:
    """The outcome of a restore, so the edge can audit it (issue #743).

    ``forced_corrupt`` is ``True`` only when an operator forced the restore of a
    known-corrupt backup over the integrity gate; ``corrupt_count`` is then the
    number of corrupt region files. A healthy restore is ``(False, 0)``.
    """

    forced_corrupt: bool
    corrupt_count: int


@dataclass(frozen=True)
class RestoreBackup:
    """Restore a backup into the authoritative copy (backup:restore, FR-BAK-4).

    Requires the server at rest: a hot replacement of a live working set is unsafe
    (Section 6.9), so a running (or transitional) server is 409. The republish is
    atomic (Storage); the restored state hydrates on the next start.

    Integrity gate (issue #743): the restore validates the extracted backup before
    publishing it. A corrupt backup (one predating the create gate #749, or an
    uploaded one) is refused by default — :class:`BackupCorruptError` propagates and
    the backup is marked ``QUARANTINED`` so an operator does not unknowingly retry
    it; ``current`` is untouched. With ``force=True`` the operator override (#703)
    publishes the corrupt working set anyway, marks the backup ``QUARANTINED`` (it
    IS known-corrupt), and the returned :class:`RestoreResult` flags the forced
    corrupt restore so the edge audits who forced it.

    After a successful restore, plugin rows are reconciled against the restored
    filesystem (issue #1336): orphan DB rows are dropped, ghost files are ingested,
    and shifted checksums are updated. The reconciliation requires ``file_store``,
    ``cache``, and ``clock``; when not provided (``None``) it is skipped so existing
    callers without plugin support are unaffected.
    """

    uow: UnitOfWork
    backup_store: BackupArchiveStore
    lifecycle_lock: LifecycleLock = NullLifecycleLock()
    file_store: FileStore | None = None
    cache: PluginCacheStore | None = None
    clock: Clock | None = None

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        backup_id: BackupId,
        force: bool = False,
    ) -> RestoreResult:
        # Hold the per-server lifecycle lock across the at-rest check, the Storage
        # republish, and the quarantine commit (issue #827): a start that flips
        # desired=running must serialize with this restore, not race the
        # ``current`` republish underneath it. The lock spans the two transactions
        # the at-rest check and the (re)publish straddle.
        async with self.lifecycle_lock.hold(server_id):
            async with self.uow:
                server = await _load(self.uow, community_id, server_id)
                backup = await self.uow.backups.get_by_id(backup_id)
                if backup is None or backup.server_id != server_id:
                    raise BackupNotFoundError(str(backup_id.value))
                storage_ref = backup.storage_ref
            if not server.is_at_rest():
                raise ServerNotStoppedError(str(server_id.value))
            try:
                corrupt_count = await self.backup_store.restore(
                    community_id=community_id,
                    server_id=server_id,
                    storage_ref=storage_ref,
                    force=force,
                )
            except BackupCorruptError:
                # Refused restore (corrupt, no force): quarantine the backup so an
                # operator sees it is corrupt, then re-raise for the edge to surface
                # and audit. ``current`` was left untouched by Storage.
                await self._quarantine(backup_id)
                raise
            if corrupt_count > 0:
                # Forced restore of a known-corrupt backup: it published, but the
                # backup IS corrupt, so quarantine it; the edge audits the forced
                # corrupt restore.
                await self._quarantine(backup_id)
                return RestoreResult(forced_corrupt=True, corrupt_count=corrupt_count)
            # Reconcile plugin rows against the restored filesystem (#1336).
            if self.file_store is not None:
                await _reconcile_plugins(
                    uow=self.uow,
                    file_store=self.file_store,
                    cache=self.cache,
                    clock=self.clock,
                    community_id=community_id,
                    server_id=server_id,
                    server=server,
                )
            return RestoreResult(forced_corrupt=False, corrupt_count=0)

    async def _quarantine(self, backup_id: BackupId) -> None:
        async with self.uow:
            await self.uow.backups.update_health(backup_id, BackupHealth.QUARANTINED)
            await self.uow.commit()


async def _reconcile_plugins(
    *,
    uow: UnitOfWork,
    file_store: FileStore,
    cache: PluginCacheStore | None,
    clock: Clock | None,
    community_id: CommunityId,
    server_id: ServerId,
    server: Server,
) -> None:
    """Reconcile ``server_plugin`` rows against the restored filesystem (#1336).

    After a backup restore replaces the working set, plugin rows may be stale:

    1. **Orphans** -- DB rows whose ``rel_path`` file no longer exists on disk.
       Deleted.
    2. **Ghosts** -- ``.jar`` files on disk with no matching DB row. Ingested
       with manifest parsing.
    3. **Shifted** -- DB rows where the file exists but its checksum changed.
       Updated with new checksums and re-parsed manifest metadata.

    Errors during ghost ingestion or manifest parsing for a single file are
    logged and skipped (the restore must not fail for a bad jar).
    """

    try:
        content_dir = content_dir_for_server_type(server.server_type)
    except UnsupportedPluginServerTypeError:
        return  # Vanilla/Spigot: no plugin content directory.

    async with uow:
        db_plugins = await uow.plugins.list_for_server(server_id)

        # Build a map of rel_path -> plugin for quick lookup.
        db_by_path: dict[str, ServerPlugin] = {p.rel_path: p for p in db_plugins}

        # Scan the content directory for .jar files on disk after restore.
        try:
            entries = await file_store.list_dir(
                community_id=community_id, server_id=server_id, rel_path=content_dir
            )
        except Exception:  # noqa: BLE001 - content dir may not exist
            entries = []

        disk_jars: set[str] = set()
        for entry in entries:
            if not entry.is_dir and entry.name.lower().endswith(".jar"):
                disk_jars.add(f"{content_dir}/{entry.name}")

        changed = False

        # 1. Drop orphans: DB rows whose file is gone.
        for plugin in db_plugins:
            if plugin.rel_path not in disk_jars:
                await uow.plugins.delete(plugin.id)
                changed = True

        # 2. Ingest ghosts: files on disk with no DB row.
        for jar_path in sorted(disk_jars):
            if jar_path in db_by_path:
                continue
            try:
                jar_bytes = await file_store.read_file(
                    community_id=community_id,
                    server_id=server_id,
                    rel_path=jar_path,
                )
            except Exception:  # noqa: BLE001
                _LOG.warning("plugin reconcile: could not read %s; skipping", jar_path)
                continue
            try:
                await _ingest_ghost(
                    uow=uow,
                    cache=cache,
                    clock=clock,
                    server=server,
                    server_id=server_id,
                    content_dir=content_dir,
                    jar_path=jar_path,
                    jar_bytes=jar_bytes,
                )
            except Exception:  # noqa: BLE001
                _LOG.warning(
                    "plugin reconcile: failed to ingest %s; skipping", jar_path
                )
                continue
            changed = True

        # 3. Update shifted records: file exists but checksum changed.
        for plugin in db_plugins:
            if plugin.rel_path not in disk_jars:
                continue  # Already deleted as orphan.
            try:
                jar_bytes = await file_store.read_file(
                    community_id=community_id,
                    server_id=server_id,
                    rel_path=plugin.rel_path,
                )
            except Exception:  # noqa: BLE001
                continue
            new_checksum = hashlib.sha512(jar_bytes).hexdigest()
            if new_checksum == plugin.checksum_sha512:
                continue
            # Content changed: update checksums and re-parse manifest.
            plugin.checksum_sha512 = new_checksum
            plugin.size_bytes = len(jar_bytes)
            if cache is not None:
                try:
                    plugin.sha256 = await ingest_into_cache(cache, jar_bytes)
                except Exception:  # noqa: BLE001
                    pass
            try:
                loader = modrinth_loader_for_server_type(server.server_type)
                manifest = parse_manifest_at_ingest(jar_bytes, loader=loader)
                plugin.mod_identifier = manifest.mod_identifier or None
                plugin.provides = manifest.provides
                plugin.dependencies = manifest.dependencies
                plugin.mc_versions = manifest.mc_versions
                plugin.side = manifest.side
            except Exception:  # noqa: BLE001
                pass
            if clock is not None:
                plugin.updated_at = clock.now()
            await uow.plugins.update(plugin)
            changed = True

        if changed:
            await uow.commit()


async def _ingest_ghost(
    *,
    uow: UnitOfWork,
    cache: PluginCacheStore | None,
    clock: Clock | None,
    server: Server,
    server_id: ServerId,
    content_dir: str,
    jar_path: str,
    jar_bytes: bytes,
) -> None:
    """Create a new plugin row for a ghost file on disk (#1336)."""

    filename = jar_path.removeprefix(f"{content_dir}/")
    loader_type = loader_type_for_server_type(server.server_type)
    loader = modrinth_loader_for_server_type(server.server_type)
    manifest = parse_manifest_at_ingest(jar_bytes, loader=loader)
    checksum = hashlib.sha512(jar_bytes).hexdigest()

    sha256: str | None = None
    if cache is not None:
        sha256 = await ingest_into_cache(cache, jar_bytes)

    ts = clock.now() if clock is not None else dt.datetime.now(dt.timezone.utc)

    plugin = ServerPlugin(
        id=PluginId.new(),
        server_id=server_id,
        rel_path=jar_path,
        filename=filename,
        display_name=manifest.mod_identifier or filename.removesuffix(".jar"),
        description=None,
        loader_type=loader_type,
        source=PluginSource.LOCAL,
        source_project_id=None,
        source_version_id=None,
        version_number=None,
        checksum_sha512=checksum,
        sha256=sha256,
        size_bytes=len(jar_bytes),
        enabled=True,
        installed_by=None,
        created_at=ts,
        updated_at=ts,
        mod_identifier=manifest.mod_identifier or None,
        provides=manifest.provides,
        dependencies=manifest.dependencies,
        mc_versions=manifest.mc_versions,
        side=manifest.side,
    )
    await uow.plugins.add(plugin)


@dataclass(frozen=True)
class DeleteBackup:
    """Delete a backup's archive then its metadata row (backup:delete).

    Archive first, metadata last (see module docstring): a crash between the two
    leaves a metadata row whose archive is gone (harmless, re-deletable), never an
    orphaned archive with no row to find it by. Storage delete is idempotent.
    """

    uow: UnitOfWork
    backup_store: BackupArchiveStore
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        backup_id: BackupId,
    ) -> None:
        # Hold the per-server lifecycle lock across the archive delete and the row
        # delete (issue #827): a concurrent restore that republishes from this same
        # backup, or a delete-server pruning the working set, must serialize with
        # the archive removal rather than race it across the two transactions.
        async with self.lifecycle_lock.hold(server_id):
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
            # An off-host archive bypasses the create-direction integrity gate, so
            # its structural health is unknown until the sweep (#744) checks it (#742).
            health=BackupHealth.UNKNOWN,
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

    Lazily backfills legacy NULL ``size_bytes`` rows whose archive still exists
    (issue #661), so once backfilled they join the total rather than staying an
    honest-but-partial unknown.
    """

    uow: UnitOfWork
    backup_store: BackupArchiveStore

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> BackupStatistics:
        async with self.uow:
            await _load(self.uow, community_id, server_id)
            rows = await self.uow.backups.list_for_server(server_id)
            rows = await _backfill_null_sizes(
                self.uow, self.backup_store, community_id, server_id, rows
            )
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


async def _backfill_null_sizes(
    uow: UnitOfWork,
    backup_store: BackupArchiveStore,
    community_id: CommunityId,
    server_id: ServerId,
    rows: list[Backup],
) -> list[Backup]:
    """Compute + persist ``size_bytes`` for legacy NULL rows whose archive exists.

    The lazy backfill on read (issue #661). Only NULL-size rows trigger a store
    call, and the persisted value makes it one-time per row. Best-effort, and the
    listing must never fail on a storage probe: if the archive is gone
    (``BackupNotFoundError``, the quiet expected case) or the store probe fails
    for any other reason — a non-404 object-store error, a connection failure, an
    fs ``OSError`` — the row is left NULL (an honest "unknown") and the remaining
    rows are still backfilled. Unexpected failures are logged WARN so an operator
    can see a degraded backfill during a storage outage. Runs inside the caller's
    open unit of work; the returned ``rows`` carry any computed size so the
    listing/total reflect it immediately.
    """

    changed = False
    for row in rows:
        if row.size_bytes is None:
            try:
                size_bytes = await backup_store.size(
                    community_id=community_id,
                    server_id=server_id,
                    storage_ref=row.storage_ref,
                )
            except BackupNotFoundError:
                continue
            except Exception as exc:  # noqa: BLE001 - best-effort storage probe
                _LOG.warning(
                    "backfill of backup %s size failed; leaving it unknown: %r",
                    row.id.value,
                    exc,
                )
                continue
            await uow.backups.update_size(row.id, size_bytes)
            row.size_bytes = size_bytes
            changed = True
    if changed:
        await uow.commit()
    return rows


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
