"""Backup-management use cases with state branching (Section 6.9, 6.11).

These run after the route's authorization dependency admitted the caller, so they
assume an authorized member and only do the backup work. Create branches on server
state per the 6.9 table:

- **at rest** (``server.is_at_rest()``: desired=stopped, observed in
  {stopped, unknown, crashed}) -> archive directly from the authoritative
  Storage copy, through the :class:`BackupArchiveStore` seam (FR-BAK-2). The
  server must also be released (``server.is_quiescent()``): while a stop still
  holds its Worker, ``current`` lacks the final snapshot in flight, so the create
  is refused 409 ``server_assigned`` (issue #3998). Restore is refused the same
  way, since that snapshot would publish over it.
- **running** (desired=running, observed=running, a worker assigned) ->
  on-demand snapshot (the :class:`SnapshotServer` hook, PR #114) whose worker
  path quiesces safely (save-off + non-blocking save-all + settle-wait), so the
  live working set is published to the authoritative copy, then archive that
  fresh snapshot. The archive always reads the authoritative copy; the running
  path only makes that copy current first (Section 6.9, FR-BAK-2).
- **anything else** (starting/stopping/restarting/crashed, or a desired/observed
  mismatch) -> :class:`BackupUnsettledError` (409): neither source is well-defined.

Each create writes the archive **before** it records its :class:`Backup` metadata
row (source manual or scheduled, the archive ref, its size, the actor), and an
upload stores its archive the same way (issue #4086). The row is inserted with
its size in one transaction, so no row is recorded ahead of its archive. A
failure after the archive write and before the row's COMMIT is sent, a
cancellation included, deletes the archive again (best effort). Once the COMMIT
is sent, its outcome is unknown, since a COMMIT can apply and still raise: the
archive is kept and a WARNING logged. A crash, a delete that fails, or an
unconfirmed COMMIT that did not apply leaves an archive with no row: no listing
shows it, and the server's delete prunes it.

Restore requires the server **at rest** (FR-BAK-4): a hot replacement of a live
working set is unsafe, so a running server is 409. Restore republishes the
archive into the authoritative copy atomically; the restored state hydrates on the
next start with no extra work (hydrate reads ``current``).

Delete removes both the archive and the metadata row. Ordering: the **archive is
deleted first, the metadata row last** — so a crash between the two leaves a
metadata row whose archive is already gone (a dangling-pointer row), never a
metadata row deleted while its archive lingers as an unreferenced orphan with no
row to find it by. The dangling row is fixable, not harmless: delete is idempotent
and re-running it removes the row, but until then it is listed as a backup that
cannot be downloaded or restored, and retention ranks it by its health like a row
with an archive while its size is recorded (only a NULL size ranks behind, issue
#4105), so a scheduled one can keep a slot a real backup needed. ``delete_backup``
on Storage is idempotent, so the archive delete is safe to retry.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import logging
import tarfile
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import IO

from mc_server_dashboard_api.archive_member import (
    UPLOAD_HEADER_LIMITS,
    CapExceededError,
    CappedReader,
    EndCheckedReader,
    GzipReader,
    HeaderLimitError,
    unrestorable_reason,
    without_tar_root_prefix,
)
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.operations import (
    BACKUP_DELETE,
    TARGET_BACKUP,
)
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.member_name import member_name_refusal
from mc_server_dashboard_api.servers.application.archive_validation import (
    run_archive_validation,
)
from mc_server_dashboard_api.servers.application.files import (
    _TAR_GZ_READ_ERRORS,
    MAX_ARCHIVE_ENTRIES,
    MAX_DECOMPRESSED_BYTES,
    MAX_UPLOAD_BYTES,
    _refuse_dir_at_root_properties,
    _refuse_reserved_root_name,
)
from mc_server_dashboard_api.servers.application.platform_properties import (
    assigned_resource_pack,
    read_properties,
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
from mc_server_dashboard_api.servers.domain.backup_retention import (
    RetentionPolicy,
    backups_to_prune,
)
from mc_server_dashboard_api.servers.domain.backup_store import BackupArchiveStore
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.display_name import fitted_display_name
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    BackupCorruptError,
    BackupNotFoundError,
    BackupRestoreIncompleteError,
    BackupRestorePartiallyPublishedError,
    BackupUnreadableError,
    BackupUnsettledError,
    FileTooLargeError,
    InvalidBackupArchiveError,
    InvalidRetentionPolicyError,
    PluginReconcileIncompleteError,
    ServerFileNotFoundError,
    ServerNotFoundError,
    ServerNotStoppedError,
    UnsupportedPluginServerTypeError,
    WorkingSetSeedFailedError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileEntryKind, FileStore
from mc_server_dashboard_api.servers.domain.lifecycle_lock import (
    HeldLifecycleLock,
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
from mc_server_dashboard_api.servers.domain.server_properties import (
    apply_platform_properties,
    new_rcon_password,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
)

_LOG = logging.getLogger(__name__)

# How much to stream per chunk when handing the uploaded archive to Storage; one
# bounded block in flight, never the whole archive re-buffered.
_UPLOAD_STREAM_CHUNK = 1024 * 1024

# The working-set path of the file whose platform-managed keys a restore re-applies.
_PROPERTIES_REL_PATH = "server.properties"

# How many characters of an uploaded member name a refusal echoes (issue #3592): a
# PAX long name compresses to almost nothing, so an unbounded echo would amplify a
# tiny upload into a huge 422 body.
_MAX_ECHOED_NAME_CHARS = 256


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
    """Create a backup, branching at-rest -> Storage / running -> snapshot.

    ``backup:create`` (manual) and the scheduled path both run through here; the
    ``source`` and ``created_by`` differ per caller.
    """

    uow: UnitOfWork
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
        # (possibly running-path snapshot) source preparation, and the
        # archive of ``current`` (issue #827, #876): a lock-holding restore that
        # republishes ``current`` mid-tar would otherwise tear the archive (the
        # #827-class gap noted in #827). The same lock serializes this archive
        # against a concurrent restore/delete just as RestoreBackup serializes its
        # republish.
        async with self.lifecycle_lock.hold(server_id) as lock:
            async with self.uow:
                server = await _load(self.uow, community_id, server_id)

            if _is_running(server):
                await self.snapshot_server(
                    community_id=server.community_id, server_id=server.id
                )
            elif not server.is_quiescent():
                # starting / stopping / restarting / crashed / mismatch: no source.
                # At rest but still held for the final snapshot: ``current`` lacks
                # that snapshot, so archiving it now would miss it (issue #3998).
                raise server.not_quiescent_error(BackupUnsettledError)

            # Write the archive BEFORE recording its row (issue #4086), so no row
            # is recorded ahead of its archive. A failure after the write and
            # before the row's COMMIT is sent deletes the archive again (best
            # effort, :func:`_discard_archive`); after it, the archive is kept.
            storage_ref = uuid.uuid4().hex
            created_at = self.clock.now()
            # The lock may have been lost during the snapshot (issue #3927).
            await lock.ensure_held()
            # Both the at-rest path and the running path (after the snapshot just
            # published) archive the authoritative copy (Section 6.9, FR-BAK-2).
            await self.backup_store.create_from_current(
                community_id=community_id,
                server_id=server_id,
                storage_ref=storage_ref,
            )
            commit_sent = False
            try:
                backup = Backup(
                    id=BackupId.new(),
                    server_id=server_id,
                    storage_ref=storage_ref,
                    size_bytes=await self.backup_store.size(
                        community_id=community_id,
                        server_id=server_id,
                        storage_ref=storage_ref,
                    ),
                    source=source,
                    # Healthy by construction: ``create_from_current`` runs through
                    # the integrity gate (#749), which refuses to archive a corrupt
                    # working set — so reaching here means the archived contents
                    # were sound (#742).
                    health=BackupHealth.HEALTHY,
                    created_by=created_by,
                    created_at=created_at,
                )
                # The archive write can take minutes; check again before the row.
                await lock.ensure_held()
                async with self.uow:
                    # The savepoint sends the INSERT, so a refused one raises
                    # here, before the COMMIT is sent.
                    async with self.uow.savepoint():
                        await self.uow.backups.add(backup)
                    commit_sent = True
                    await self.uow.commit()
            except BaseException:
                if commit_sent:
                    _keep_unconfirmed_archive(backup)
                else:
                    await _discard_archive(
                        self.backup_store,
                        community_id=community_id,
                        server_id=server_id,
                        storage_ref=storage_ref,
                    )
                raise
            return backup


async def _discard_archive(
    backup_store: BackupArchiveStore,
    *,
    community_id: CommunityId,
    server_id: ServerId,
    storage_ref: str,
) -> None:
    """Delete an archive whose row was not recorded (issue #4086).

    Only for a failure before the row's COMMIT is sent: after it the row may
    stand, and :func:`_keep_unconfirmed_archive` keeps the archive. Best effort: a
    delete that fails is logged and the caller's own failure is raised. The
    archive left behind has no row, so no listing shows it. It runs without a
    lifecycle-lock check: the ref is new, so nothing else can be using the
    archive.
    """

    try:
        await backup_store.delete(
            community_id=community_id,
            server_id=server_id,
            storage_ref=storage_ref,
        )
    except Exception:
        _LOG.warning(
            "could not delete the archive of a backup whose row was not recorded",
            exc_info=True,
            extra={"server_id": str(server_id.value), "storage_ref": storage_ref},
        )


def _keep_unconfirmed_archive(backup: Backup) -> None:
    """Keep the archive of a row whose COMMIT was sent but not confirmed.

    A COMMIT can apply and still raise: its answer lost with the connection, a
    cancellation while awaiting it, or a failure leaving the block after it. The
    row may stand, and deleting its archive would leave it without one, the row
    issue #4086 removes. So the archive is kept and logged. If the COMMIT did not
    apply, the archive has no row, so no listing shows it.
    """

    _LOG.warning(
        "kept the archive of a backup whose row commit was not confirmed",
        extra={
            "server_id": str(backup.server_id.value),
            "storage_ref": backup.storage_ref,
            "backup_id": str(backup.id.value),
        },
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

    That probe makes this a storage-touching route: a store fault fails the listing
    rather than degrading it to null sizes (issue #2405). The decision, and which
    faults reach the edge as 503 rather than 500, are in :func:`_backfill_null_sizes`.
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

    An ``UNREADABLE`` backup (issue #2374) is refused up front with
    :class:`BackupUnreadableError`, ``force`` or not: its archive could not be read
    back at all, so the override has nothing to publish. The refusal reads the
    recorded verdict rather than the archive and leaves it unchanged — only an
    integrity sweep that reads the archive back in full revises it. A restore that
    is itself the first read to find the archive unreadable (issue #3230) is
    refused the same way and records ``UNREADABLE``, the verdict the sweep would
    have reached, so the next attempt is refused up front.

    A backup carries the working set as it was when the backup was taken, so the
    republished ``server.properties`` holds the platform-managed values of THAT
    moment — a ``server-port`` the server may since have been re-ported away from,
    a resource pack it no longer has assigned. Nothing downstream re-applies the
    DB's values, so the restore re-applies them itself
    (:func:`apply_platform_properties`, issue #2621) before anything hydrates the
    restored copy; a backup that carries no ``server.properties`` at all gets one
    holding just those keys, exactly as a create seeds it, rather than leaving the
    worker to fall back to 25565.

    After a successful restore, plugin rows are reconciled against the restored
    filesystem (issue #1336): orphan DB rows are dropped, ghost files are ingested,
    and shifted checksums are updated. The reconciliation requires ``file_store``,
    ``cache``, and ``clock``; when not provided (``None``) it is skipped so existing
    callers without plugin support are unaffected. ``file_store`` also gates the
    properties re-apply, for the same reason.

    Both post-publication steps run for EVERY restore that published, a forced
    corrupt one included (issue #3222): a corrupt world region says nothing about
    the jars beside it. A restored plugin directory that cannot be listed raises
    :class:`PluginReconcileIncompleteError` with the rows untouched (issue #3221),
    and so does a listed jar that cannot be read (issue #3269). Any failure after
    the publish — a store failure past its flip (issue #3564), the quarantine
    write, and an error the lifecycle lock's hold raises on exit included (the
    real lock logs a lost session there instead of raising, issue #3927) —
    surfaces as :class:`BackupRestoreIncompleteError`, wrapping the step's own
    error and carrying the corrupt count of a forced corrupt backup that
    published, so the edge audits the force-restore whatever failed after it.
    A store that fails past its flip has published, so both steps still run over
    the restored working set; one of them failing is then the answer, with the
    store's failure logged.
    """

    uow: UnitOfWork
    backup_store: BackupArchiveStore
    lifecycle_lock: LifecycleLock = NullLifecycleLock()
    file_store: FileStore | None = None
    cache: PluginCacheStore | None = None
    clock: Clock | None = None
    # The deployment's public base URL, which the restored ``resource-pack`` line
    # points at (the same value :class:`AssignResourcePack` writes).
    public_base_url: str = ""
    # Fills in ``rcon.password`` when the restored file has none; a non-empty one
    # in the backup is preserved (the file is that credential's only source of
    # truth). Injected so tests are deterministic.
    token_generator: Callable[[], str] = field(default=new_rcon_password)

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
        # The corrupt count once Storage has published (``None`` before): the
        # lock's hold exits after the publish, so an error it raises on exit is a
        # post-publish failure too and keeps the restore's identity (issue #3269)
        # -- it never replaces a step failure already on its way out. The real
        # lock raises none for a lost session (issue #3927); only another error
        # from the hold's exit still reaches this.
        published: int | None = None
        incomplete: BackupRestoreIncompleteError | None = None
        partial: BackupRestorePartiallyPublishedError | None = None
        try:
            async with self.lifecycle_lock.hold(server_id) as lock:
                async with self.uow:
                    server = await _load(self.uow, community_id, server_id)
                    backup = await self.uow.backups.get_by_id(backup_id)
                    if backup is None or backup.server_id != server_id:
                        raise BackupNotFoundError(str(backup_id.value))
                    if backup.health is BackupHealth.UNREADABLE:
                        raise BackupUnreadableError(str(backup_id.value))
                    storage_ref = backup.storage_ref
                if not server.is_quiescent():
                    raise server.not_quiescent_error(ServerNotStoppedError)
                try:
                    # The store confirms the lock under its own per-server lock,
                    # after the slow staging and right before it publishes (issue
                    # #3927): a check here would leave the whole staging unguarded.
                    corrupt_count = await self.backup_store.restore(
                        community_id=community_id,
                        server_id=server_id,
                        storage_ref=storage_ref,
                        force=force,
                        fence=lock.ensure_held,
                    )
                except BackupCorruptError:
                    # Refused restore (corrupt, no force): quarantine the backup so an
                    # operator sees it is corrupt, then re-raise for the edge to surface
                    # and audit. ``current`` was left untouched by Storage.
                    await self._mark_health(lock, backup_id, BackupHealth.QUARANTINED)
                    raise
                except BackupUnreadableError:
                    # The restore was the read that found the archive's bytes gone
                    # (issue #3230). Record the verdict the sweep would have, so the
                    # next attempt is refused up front instead of re-reading the
                    # archive; a sweep that later reads it back sound revises it.
                    await self._mark_health(lock, backup_id, BackupHealth.UNREADABLE)
                    raise
                except BackupRestorePartiallyPublishedError as exc:
                    # The store published the backup, then failed before its
                    # restore returned, or cannot tell whether it published (issue
                    # #3564). The restored working set is (or may be) live, so the
                    # steps below still run over it; they are harmless over the
                    # prior one. The store's failure is raised once they have.
                    partial = exc
                    corrupt_count = exc.corrupt_count
                published = corrupt_count
                # Published: the restored working set stands whatever fails from here
                # on, so every later step's failure carries the restore's identity —
                # above all a forced corrupt one, which the edge must still audit as
                # the force-restore it was (issue #3269).
                try:
                    await self._after_publish(
                        lock,
                        community_id=community_id,
                        server_id=server_id,
                        server=server,
                        backup_id=backup_id,
                        corrupt_count=corrupt_count,
                    )
                except Exception as exc:
                    if partial is not None:
                        _LOG.warning(
                            "the store failed after publishing a restore",
                            exc_info=partial,
                            extra={"server_id": str(server_id.value)},
                        )
                    incomplete = BackupRestoreIncompleteError(
                        str(server_id.value),
                        cause=exc,
                        forced_corrupt_count=corrupt_count,
                    )
                    raise incomplete from exc
                if partial is not None:
                    incomplete = BackupRestoreIncompleteError(
                        str(server_id.value),
                        cause=partial,
                        forced_corrupt_count=corrupt_count,
                    )
                    raise incomplete from partial
                return RestoreResult(
                    forced_corrupt=corrupt_count > 0, corrupt_count=corrupt_count
                )
        except Exception as exc:
            if published is None or exc is incomplete:
                raise
            if incomplete is not None:
                _LOG.warning(
                    "releasing the lifecycle lock after an incomplete restore failed",
                    exc_info=exc,
                    extra={"server_id": str(server_id.value)},
                )
                raise incomplete
            raise BackupRestoreIncompleteError(
                str(server_id.value), cause=exc, forced_corrupt_count=published
            ) from exc

    async def _after_publish(
        self,
        lock: HeldLifecycleLock,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        server: Server,
        backup_id: BackupId,
        corrupt_count: int,
    ) -> None:
        if corrupt_count > 0:
            # Forced restore of a known-corrupt backup: it published, but the
            # backup IS corrupt, so quarantine it; the edge audits the forced
            # corrupt restore. Quarantine BEFORE the properties re-apply, so a
            # failure there cannot leave a known-corrupt backup unmarked.
            await self._mark_health(lock, backup_id, BackupHealth.QUARANTINED)
        # The properties re-apply is an in-place working-set write that relies on
        # the lock (issue #3927). The plugin reconcile, which rewrites rows from
        # that working set, checks again before its commit.
        await lock.ensure_held()
        await self._reapply_platform_properties(
            community_id=community_id, server_id=server_id, server=server
        )
        # Reconcile plugin rows against the restored filesystem (#1336).
        if self.file_store is not None:
            await _reconcile_plugins(
                lock,
                uow=self.uow,
                file_store=self.file_store,
                cache=self.cache,
                clock=self.clock,
                community_id=community_id,
                server_id=server_id,
                server=server,
            )

    async def _mark_health(
        self, lock: HeldLifecycleLock, backup_id: BackupId, health: BackupHealth
    ) -> None:
        # A verdict is written under the lock, as the integrity sweep writes its
        # own (issue #3927).
        await lock.ensure_held()
        async with self.uow:
            await self.uow.backups.update_health(backup_id, health)
            await self.uow.commit()

    async def _reapply_platform_properties(
        self, *, community_id: CommunityId, server_id: ServerId, server: Server
    ) -> None:
        """Rewrite the restored ``server.properties`` platform keys (issue #2621).

        The restore republished the backup's copy of the file verbatim, so every
        platform-managed key currently holds the value it had when the backup was
        taken. Re-apply what the DB says now: the tracked ``game_port``, the RCON
        keys, and the resource-pack keys from the assignment row (cleared when the
        server has none). An absent file is written from empty, which yields the
        same platform-keys-only file a create seeds — the alternative would leave
        the worker falling back to 25565, the collision this issue is about.

        A storage failure here surfaces as :class:`WorkingSetSeedFailedError` (503),
        the same posture the port ``PATCH`` and the import publish take: the world
        data IS restored, but the working set is not fully seeded, and re-running
        the restore heals it. Failing loudly beats returning success over a file
        that binds the wrong port.
        """

        if self.file_store is None:
            return
        try:
            current = await read_properties(
                self.file_store, community_id=community_id, server_id=server_id
            )
        except Exception as exc:
            raise WorkingSetSeedFailedError(str(server_id.value)) from exc
        try:
            await self.file_store.write_file(
                community_id=community_id,
                server_id=server_id,
                rel_path=_PROPERTIES_REL_PATH,
                content=apply_platform_properties(
                    current if current is not None else b"",
                    game_port=server.game_port,
                    rcon_password=self.token_generator(),
                    resource_pack=await assigned_resource_pack(
                        self.uow,
                        server_id=server_id,
                        public_base_url=self.public_base_url,
                    ),
                ),
            )
        except Exception as exc:
            _LOG.warning(
                "re-applying the platform-managed server.properties keys after a "
                "restore failed; the restored file may bind a stale port",
                extra={"server_id": str(server_id.value)},
            )
            raise WorkingSetSeedFailedError(str(server_id.value)) from exc


async def _reconcile_plugins(
    lock: HeldLifecycleLock,
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
    logged and skipped (the restore must not fail for a bad jar). A listed jar
    gone by its read (the typed miss) is skipped too.

    The listing of the content directory is the evidence every step above rests
    on, so only the typed miss (:class:`ServerFileNotFoundError`) reads as "no
    jars". Any other failure to list it — a store outage, an I/O or permission
    error — raises :class:`PluginReconcileIncompleteError` before a single row is
    touched (issue #3221): treating an unread directory as an empty one deleted
    every tracked row, provenance included, while the jars were still there.
    A listed jar whose read fails any other way raises it too (issue #3269): the
    jar is not known to be bad, only unread, so skipping it would report a partial
    reconciliation as complete. The transaction is discarded, not committed.

    The commit rewrites rows from the restored working set, so it runs only
    after ``lock`` confirms the lifecycle lock is still held (issue #3927): the
    listing and the jar reads before it can take long on a large modpack.
    """

    try:
        content_dir = content_dir_for_server_type(server.server_type)
    except UnsupportedPluginServerTypeError:
        return  # Vanilla: no plugin content directory.

    async with uow:
        db_plugins = await uow.plugins.list_for_server(server_id)

        # Build a map of rel_path -> plugin for quick lookup.
        db_by_path: dict[str, ServerPlugin] = {p.rel_path: p for p in db_plugins}

        # Scan the content directory for .jar (and .jar.disabled) files on
        # disk after restore. Disabled plugins use a ``.jar.disabled`` suffix
        # (issue #1308), so both must be included to avoid deleting disabled
        # plugin rows as orphans.
        try:
            entries = await file_store.list_dir(
                community_id=community_id, server_id=server_id, rel_path=content_dir
            )
        except ServerFileNotFoundError:
            entries = []  # No content directory in the restored working set.
        except Exception as exc:
            _LOG.warning(
                "plugin reconcile: could not list %s after a restore; plugin rows "
                "left unreconciled",
                content_dir,
                extra={"server_id": str(server_id.value)},
            )
            raise PluginReconcileIncompleteError(str(server_id.value)) from exc

        disk_jars: set[str] = set()
        for entry in entries:
            if entry.kind is FileEntryKind.DIRECTORY:
                continue
            lower = entry.name.lower()
            if lower.endswith(".jar") or lower.endswith(".jar.disabled"):
                disk_jars.add(f"{content_dir}/{entry.name}")

        changed = False

        # 1. Drop orphans: DB rows whose file is gone.  Client-only plugins
        #    (side == "client") are excluded: they have no working-set file, so
        #    they cannot be matched against the restored filesystem (#1445).
        for plugin in db_plugins:
            if plugin.side == "client":
                continue
            if plugin.rel_path not in disk_jars:
                await uow.plugins.delete(plugin.id)
                changed = True

        # 2. Ingest ghosts: files on disk with no DB row.
        for jar_path in sorted(disk_jars):
            if jar_path in db_by_path:
                continue
            jar_bytes = await _read_listed_jar(
                file_store,
                community_id=community_id,
                server_id=server_id,
                rel_path=jar_path,
            )
            if jar_bytes is None:
                continue
            try:
                # In a savepoint, so a refused INSERT (a racing install taking
                # this path, the server deleted mid-restore) discards only this
                # jar. Left unscoped, the refusal deactivated the transaction and
                # every later statement failed with a PendingRollbackError
                # naming an innocent jar, several statements from the cause
                # (issue #2612).
                async with uow.savepoint():
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
            jar_bytes = await _read_listed_jar(
                file_store,
                community_id=community_id,
                server_id=server_id,
                rel_path=plugin.rel_path,
            )
            if jar_bytes is None:
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
            await lock.ensure_held()
            await uow.commit()


async def _read_listed_jar(
    file_store: FileStore,
    *,
    community_id: CommunityId,
    server_id: ServerId,
    rel_path: str,
) -> bytes | None:
    """Read a jar the listing named; ``None`` if it is gone by now (issue #3269).

    Any other failure is a read that never happened, not a bad jar, so it raises
    :class:`PluginReconcileIncompleteError`, as a failed listing does (#3221).
    """

    try:
        return await file_store.read_file(
            community_id=community_id, server_id=server_id, rel_path=rel_path
        )
    except ServerFileNotFoundError:
        _LOG.warning("plugin reconcile: %s is gone; skipping", rel_path)
        return None
    except Exception as exc:
        _LOG.warning(
            "plugin reconcile: could not read %s after a restore; plugin rows "
            "left unreconciled",
            rel_path,
            extra={"server_id": str(server_id.value)},
        )
        raise PluginReconcileIncompleteError(str(server_id.value)) from exc


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

    raw_name = jar_path.removeprefix(f"{content_dir}/")
    # A .jar.disabled file is a disabled plugin; strip the suffix for the
    # clean filename and derive enabled from it.
    disabled = raw_name.lower().endswith(".jar.disabled")
    filename = raw_name.removesuffix(".disabled") if disabled else raw_name
    loader_type = loader_type_for_server_type(server.server_type)
    loader = modrinth_loader_for_server_type(server.server_type)
    manifest = parse_manifest_at_ingest(jar_bytes, loader=loader)
    checksum = hashlib.sha512(jar_bytes).hexdigest()

    sha256: str | None = None
    if cache is not None:
        sha256 = await ingest_into_cache(cache, jar_bytes)

    ts = clock.now() if clock is not None else dt.datetime.now(dt.timezone.utc)

    # Recover provenance by checksum (issue #2059): a restored filesystem carries
    # jars, not the DB rows that recorded their origin, so a catalog-installed jar
    # re-ingested here would otherwise be asserted LOCAL and drop out of update
    # checking. Match its checksum against any catalog install elsewhere to restore
    # source / source_project_id; on a miss, mark it UNKNOWN rather than silently
    # claiming a manual upload.
    recovered = await uow.plugins.find_catalog_provenance_by_sha512(checksum)
    if recovered is not None:
        source, source_project_id, source_version_id, version_number = recovered
    else:
        source = PluginSource.UNKNOWN
        source_project_id = None
        source_version_id = None
        version_number = None

    plugin = ServerPlugin(
        id=PluginId.new(),
        server_id=server_id,
        rel_path=jar_path,
        filename=filename,
        # Neither the mod id nor the file's name was typed by the user, so a name
        # the display-name rule refuses is made to fit it (issue #3988).
        display_name=fitted_display_name(
            manifest.mod_identifier or "", filename=filename
        ),
        description=None,
        loader_type=loader_type,
        source=source,
        source_project_id=source_project_id,
        source_version_id=source_version_id,
        version_number=version_number,
        checksum_sha512=checksum,
        sha256=sha256,
        size_bytes=len(jar_bytes),
        enabled=not disabled,
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
    leaves a metadata row whose archive is gone (re-deletable, but not harmless
    until then), never an orphaned archive with no row to find it by. Storage
    delete is idempotent.
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
        async with self.lifecycle_lock.hold(server_id) as lock:
            async with self.uow:
                await _load(self.uow, community_id, server_id)
                backup = await self.uow.backups.get_by_id(backup_id)
                if backup is None or backup.server_id != server_id:
                    raise BackupNotFoundError(str(backup_id.value))
                storage_ref = backup.storage_ref
            await _delete_archive_then_row(
                self.uow,
                self.backup_store,
                lock,
                community_id=community_id,
                server_id=server_id,
                backup_id=backup_id,
                storage_ref=storage_ref,
            )


async def _delete_archive_then_row(
    uow: UnitOfWork,
    backup_store: BackupArchiveStore,
    lock: HeldLifecycleLock,
    *,
    community_id: CommunityId,
    server_id: ServerId,
    backup_id: BackupId,
    storage_ref: str,
) -> None:
    """Delete a backup's archive first, then its metadata row (module docstring).

    The single delete path (epic #649 constraint): :class:`DeleteBackup` and the
    retention prune (:class:`PruneScheduledBackups`, issue #1841) both run
    through here, each under the per-server lifecycle lock it already holds.
    The archive delete is idempotent, so a crash between the two leaves a
    re-deletable dangling row (not harmless until then, see the module
    docstring), never an orphaned archive. Both run only once ``lock`` confirms
    the lifecycle lock is still held (issue #3927).
    """

    await lock.ensure_held()
    await backup_store.delete(
        community_id=community_id,
        server_id=server_id,
        storage_ref=storage_ref,
    )
    async with uow:
        await uow.backups.delete(backup_id)
        await uow.commit()


@dataclass(frozen=True)
class PruneScheduledBackups:
    """Prune ``source=scheduled`` backups per the server's retention policy (#1841).

    Loads the server's persisted ``backup_retention`` policy and deletes the
    scheduled rows the pure :func:`backups_to_prune` selection names — manual /
    uploaded / event rows are never candidates. A server without a policy (or,
    defensively, with a malformed persisted one) is a no-op.

    Before the selection ranks them, the scheduled rows with a NULL
    ``size_bytes`` are sized the way the listing sizes them
    (:func:`_backfill_null_sizes`, issue #4107), with no unit of work open, and
    their sizes recorded: a legacy row whose archive exists then ranks as the
    sized row it is. A row whose archive the store reports missing stays NULL.
    Any other store failure stops the prune before it selects or deletes
    anything, with a WARNING, rather than rank on partial information; the next
    run retries.

    Runs under the same per-server :class:`LifecycleLock` that
    :class:`RestoreBackup` / :class:`DeleteBackup` hold, so a backup can never
    be deleted while a restore of it is in flight; each deletion reuses the
    archive-first/row-last :func:`_delete_archive_then_row` core (no second
    delete path). Each deletion is audited post-commit as ``backup:delete``
    with actor ``None`` (no operator is behind a retention prune — the
    integrity-sweep posture); the retention cause is logged. Prune emits
    nothing on the notification stream (owner spec: notifications are for run
    failures only), and its triggers (the schedule runner, the policy write)
    isolate a prune failure so it never fails the operation that fired it.
    """

    uow: UnitOfWork
    backup_store: BackupArchiveStore
    audit: AuditRecorder
    clock: Clock
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> list[Backup]:
        async with self.lifecycle_lock.hold(server_id) as lock:
            async with self.uow:
                server = await _load(self.uow, community_id, server_id)
                rows = await self.uow.backups.list_for_server(server_id)
            if server.backup_retention is None:
                return []
            try:
                policy = RetentionPolicy.from_json(server.backup_retention)
            except InvalidRetentionPolicyError:
                # Should be impossible (the write path validates), but if a bad
                # value slips in, skip rather than prune the wrong set; log it
                # for an operator to fix (the schedule_from_config posture).
                _LOG.warning(
                    "server %s has an invalid backup retention policy; skipping prune",
                    server_id.value,
                )
                return []
            scheduled = [b for b in rows if b.source is BackupSource.SCHEDULED]
            try:
                scheduled = await _backfill_null_sizes(
                    self.uow, self.backup_store, community_id, server_id, scheduled
                )
            except Exception as exc:
                _LOG.warning(
                    "retention prune of server %s stopped before selecting "
                    "anything: a NULL-size scheduled backup could not be sized "
                    "(%s: %s), and the ranking needs its size. The next run "
                    "retries.",
                    server_id.value,
                    type(exc).__name__,
                    exc,
                )
                raise
            pruned = backups_to_prune(policy, scheduled, self.clock.now())
            for backup in pruned:
                await _delete_archive_then_row(
                    self.uow,
                    self.backup_store,
                    lock,
                    community_id=community_id,
                    server_id=server_id,
                    backup_id=backup.id,
                    storage_ref=backup.storage_ref,
                )
                _LOG.info(
                    "retention prune: deleted scheduled backup %s of server %s "
                    "(created %s, health %s, policy %s)",
                    backup.id.value,
                    server_id.value,
                    backup.created_at.isoformat(),
                    backup.health.value,
                    policy.to_json(),
                )
                await self.audit.record(
                    AuditEvent(
                        operation=BACKUP_DELETE,
                        outcome=Outcome.SUCCESS,
                        actor_id=None,
                        community_id=community_id.value,
                        target_type=TARGET_BACKUP,
                        target_id=backup.id.value,
                    )
                )
            return pruned


@dataclass(frozen=True)
class SetBackupRetention:
    """Set a server's backup retention policy (backup:schedule, issue #1841).

    Validates the policy fields into a :class:`RetentionPolicy`
    (:class:`InvalidRetentionPolicyError` -> 422 at the edge), persists the
    canonical JSON shape on the server row, then prunes immediately (owner
    spec: prune on policy write). The prune is best-effort — the policy write
    is the operation, so a prune failure is logged, not raised; the next
    successful scheduled backup run re-prunes.
    """

    uow: UnitOfWork
    prune: PruneScheduledBackups

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        keep_last: int | None = None,
        daily: int | None = None,
        weekly: int | None = None,
        monthly: int | None = None,
    ) -> RetentionPolicy:
        policy = RetentionPolicy.from_fields(
            keep_last=keep_last, daily=daily, weekly=weekly, monthly=monthly
        )
        async with self.uow:
            await _load(self.uow, community_id, server_id)
            await self.uow.servers.update_backup_retention(server_id, policy.to_json())
            await self.uow.commit()
        try:
            await self.prune(community_id=community_id, server_id=server_id)
        except Exception:  # noqa: BLE001 - best-effort: the policy write landed
            _LOG.exception(
                "retention prune after policy write failed for server %s",
                server_id.value,
            )
        return policy


@dataclass(frozen=True)
class ClearBackupRetention:
    """Clear a server's backup retention policy (backup:schedule, issue #1841).

    Sets ``backup_retention`` back to ``NULL`` — scheduled backups then
    accumulate unbounded again. Nothing to prune on clear.
    """

    uow: UnitOfWork

    async def __call__(self, *, community_id: CommunityId, server_id: ServerId) -> None:
        async with self.uow:
            await _load(self.uow, community_id, server_id)
            await self.uow.servers.update_backup_retention(server_id, None)
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


def download_grant_resource(
    community_id: uuid.UUID, server_id: uuid.UUID, backup_id: uuid.UUID
) -> str:
    """The opaque resource string a backup download grant is bound to (#2313).

    Binds the whole triple, so a grant minted for one backup opens neither
    another backup nor the same backup id addressed under a different server or
    community. The identity Port treats this as an opaque string. Takes the raw
    path ids because both the minting route and the redeeming dependency see the
    triple as URL segments.
    """

    return f"backup-download:{community_id}:{server_id}:{backup_id}"


@dataclass(frozen=True)
class ResolveBackup:
    """Load one community-scoped backup's metadata (issue #2313).

    Exists so the download-grant mint endpoint can 404 an unknown or cross-server
    backup id through the exact same lookup the download itself uses — a grant is
    never minted for a backup the caller could not have downloaded.
    """

    uow: UnitOfWork

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        backup_id: BackupId,
    ) -> Backup:
        return await _load_backup(self.uow, community_id, server_id, backup_id)


@dataclass(frozen=True)
class DownloadBackup:
    """Stream a backup archive in its native format (backup:read, issue #281).

    Resolves the (community-scoped) backup, then opens a read stream over the
    stored archive bytes through the :class:`BackupArchiveStore` seam — no
    recompression, the exact stored ``tar.gz`` bytes. An unknown / cross-server
    backup is :class:`BackupNotFoundError` (the edge 404s, no existence signal).

    Size and stream are separate operations (mirroring :class:`DownloadFile`) so
    the edge can resolve a ``Range`` request against the archive's size before it
    opens anything (issue #2372): an unsatisfiable range must be answered without
    a stream, and a satisfiable one opens a *ranged* stream. The size is read from
    the store, never from ``Backup.size_bytes`` — that column is nullable on
    legacy rows (#661), while the declared length must equal the streamed byte
    count exactly (issue #2312).

    The backup row is resolved ONCE, up front, and carried to both operations
    (issue #2456): :meth:`resolve` loads it while the status is still choosable,
    and :meth:`archive_size` / :meth:`archive_stream` take the loaded row rather
    than each re-reading it. Loading once cannot change what the client sees on a
    hit or a miss: a backup's ``storage_ref`` never changes for the row's lifetime
    (create / upload / restore all leave the archive immutable), and the only
    concurrent mutation — a delete — removes the archive BEFORE the row
    (archive-first, epic #649), so a delete racing the download is surfaced by the
    stream's own store miss on its first iteration regardless of whether the row is
    re-read (issue #2415). The moment that counts is therefore the single up-front
    load, exactly as PR #2454 made the archive resolution happen once at the
    status-choosable point.

    The one interleaving where loading once is observable is ``DeleteServer``'s
    reclaim (issues #777, #1707): it cascades the head backup's ROW away while
    deliberately RETAINING that head archive, so a download of the *head* backup
    racing the reclaim has no store miss to arbitrate. There the single up-front
    load serves a 200 whose body completes at the declared length (the archive is
    retained) where the old second load could have raced to a 404. That is a valid
    linearization, not a regression: 200-complete was already in the racing
    client's outcome set under two loads (a cascade landing after the second load),
    and a 404 stays reachable whenever the cascade precedes :meth:`resolve`.
    """

    uow: UnitOfWork
    backup_store: BackupArchiveStore

    async def resolve(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        backup_id: BackupId,
    ) -> Backup:
        """Load the (community-scoped) backup row once, before any byte is served.

        An unknown / cross-server backup is :class:`BackupNotFoundError` here — the
        same miss the edge 404s — resolved before anything is on the wire. The
        returned row carries the ``storage_ref`` both :meth:`archive_size` and
        :meth:`archive_stream` read, so the download reads the metadata row exactly
        once (issue #2456).
        """

        return await _load_backup(self.uow, community_id, server_id, backup_id)

    async def archive_size(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        backup: Backup,
    ) -> int:
        """The stored archive's byte count — the whole representation's length."""

        return await self.backup_store.size(
            community_id=community_id,
            server_id=server_id,
            storage_ref=backup.storage_ref,
        )

    def archive_stream(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        backup: Backup,
        byte_range: tuple[int, int] | None = None,
    ) -> AsyncIterator[bytes]:
        """Open the archive's bytes, or the inclusive ``byte_range`` slice of them."""

        return self.backup_store.open(
            community_id=community_id,
            server_id=server_id,
            storage_ref=backup.storage_ref,
            byte_range=byte_range,
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

    That last sentence is exactly why the validation also refuses a member named
    ``server.properties/…`` (:class:`InvalidFilePathError` with
    ``reason="platform_managed_path"`` -> 422, issue #2869): the restore
    republishes the members verbatim as ``current/``, so such a member is the
    operator-supplied route to the directory issue #2812 is about. See
    :func:`_validate_backup_archive` for why the refusal belongs here rather than
    at restore.

    The validation runs off the event loop thread (issue #3801), on the
    archive-validation pool it shares with the other upload doors
    (:mod:`.archive_validation`, issues #3843 and #3912), so a crafted upload can
    hold a thread of that pool but none of the default executor's. The loop still runs
    late while it validates: the validating thread holds the GIL as it
    decompresses, and with one such thread a 1 ms loop tick was measured late by
    up to 29 ms at the 99th percentile and 100 ms at most (several threads were
    not measured). The thread reads ``content``, the bytes the route read out of
    the request's spooled part, and never the spool, so the spool may close
    whenever the request ends.

    Once the body is read, a client that disconnects does not cancel the request:
    the validation runs to its end and the backup is stored. Only a shutdown
    cancels the call. A cancelled call stops waiting at once and stores nothing.
    The thread cannot be stopped: it runs on to its own end, holding ``content``
    and its pool thread until then.

    Empty gzip members and zero padding after the gzip stream add no
    decompressed bytes, so the caps do not see them. The gzip layer bounds what
    they cost instead (issue #3835): at most 10,000 gzip members, and zero
    padding skipped in bulk.

    The caps are fields so a test can inject a tiny cap and trip the guard with a
    small archive; production wiring uses the defaults.
    """

    uow: UnitOfWork
    backup_store: BackupArchiveStore
    clock: Clock
    max_bytes: int = MAX_UPLOAD_BYTES
    max_entries: int = MAX_ARCHIVE_ENTRIES
    max_decompressed_bytes: int = MAX_DECOMPRESSED_BYTES
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        content: bytes,
        created_by: uuid.UUID | None = None,
    ) -> Backup:
        # The checks below are verdicts about the body alone, so they run before
        # the lock and never extend its hold.
        if len(content) > self.max_bytes:
            raise FileTooLargeError(str(len(content)))
        # Decompression is CPU-bound (see the docstring for what the caps leave
        # unbounded): on the event loop it would stall every other request on the
        # process (issue #3801), and on the default executor a few such uploads
        # would starve everything else that runs there (issue #3843).
        await run_archive_validation(
            _validate_backup_archive,
            content,
            max_entries=self.max_entries,
            max_decompressed_bytes=self.max_decompressed_bytes,
        )

        # Hold the per-server lifecycle lock across the server check, the archive
        # write and the row commit, as CreateBackup does (issue #3219). Without it
        # a DeleteServer landing between the check and the write prunes the
        # server's storage and removes it, and the write then lands an archive no
        # delete saw. The body is already fully received, so the lock never waits
        # on the client's network.
        async with self.lifecycle_lock.hold(server_id) as lock:
            async with self.uow:
                await _load(self.uow, community_id, server_id)

            # Store the archive BEFORE recording its row, as CreateBackup does
            # (issue #4086). The archive is stored verbatim, so its size is the
            # body's and needs no read back.
            storage_ref = uuid.uuid4().hex
            backup = Backup(
                id=BackupId.new(),
                server_id=server_id,
                storage_ref=storage_ref,
                size_bytes=len(content),
                source=BackupSource.UPLOADED,
                # An off-host archive bypasses the create-direction integrity gate,
                # so its structural health is unknown until the sweep (#744) checks
                # it (#742).
                health=BackupHealth.UNKNOWN,
                created_by=created_by,
                created_at=self.clock.now(),
            )
            # Each guarded write is checked right before it, as in CreateBackup
            # (issue #3927).
            await lock.ensure_held()
            await self.backup_store.store(
                community_id=community_id,
                server_id=server_id,
                stream=_chunked(content),
                storage_ref=storage_ref,
            )
            commit_sent = False
            try:
                await lock.ensure_held()
                async with self.uow:
                    # As in CreateBackup: a refused INSERT raises before the
                    # COMMIT is sent.
                    async with self.uow.savepoint():
                        await self.uow.backups.add(backup)
                    commit_sent = True
                    await self.uow.commit()
            except BaseException:
                if commit_sent:
                    _keep_unconfirmed_archive(backup)
                else:
                    await _discard_archive(
                        self.backup_store,
                        community_id=community_id,
                        server_id=server_id,
                        storage_ref=storage_ref,
                    )
                raise
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
    honest-but-partial unknown. Sharing that probe with the listing means sharing its
    failure behaviour (issue #2405): a store fault fails the read rather than
    returning a total silently missing the rows the store could not size. See
    :func:`_backfill_null_sizes`.
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

    The lazy backfill on read (issue #661), which the retention prune also runs
    before it ranks (issue #4107). Only NULL-size rows trigger a store call, and
    the persisted value makes it one-time per row. The caller has closed its unit
    of work: each store call is a HEAD that can take as long as the store's
    timeouts allow, so none runs with a session open, which would keep a pool
    connection checked out for that long (issue #4107). The sizes are then
    written in a short unit of work of their own, opened only when there is one
    to write. The returned ``rows`` carry any computed size so the listing/total
    reflect it immediately.

    **Only a missing archive is tolerated** (``BackupNotFoundError``, a row left
    dangling before creates wrote the archive first, issue #4086): its NULL size
    stays and the remaining rows are still backfilled. **Everything else propagates
    and fails the read** (issue #2405), where a bare ``except Exception`` used to
    degrade it to a success with null sizes. A null size is indistinguishable from a
    genuinely unrecorded one, so nothing in a degraded response identified it as
    degraded; and once #2378 made an object-store outage a 503 ``storage_unavailable``
    on backup create / restore / upload / delete / download, one outage could not have
    five routes say "the store is down, retry" and the read say "here are your
    backups, some have no size". The cost is accepted knowingly: during an outage the
    read fails entirely, including the rows whose sizes were already recorded.
    Narrowing also stops the handler swallowing a programming error in the backfill
    itself, which is how a real bug here would have stayed invisible behind a 200.

    **Propagating is not the same as answering 503**, and only the object backend's
    outage is translated into one (``ObjectStoreUnavailableError`` at the seam). A
    non-miss ``OSError`` on the fs backend — the M1 default — and a non-5xx
    ``ClientError`` (a 403 ``InvalidAccessKeyId``, left untranslated on purpose as a
    permanent fault rather than a transient one) both reach the edge as a generic
    500, which is already what the five sibling routes do with them. Issue #2555
    covers closing that gap for all of them at once.
    """

    sizes: dict[BackupId, int] = {}
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
            row.size_bytes = sizes[row.id] = size_bytes
    if sizes:
        async with uow:
            for backup_id, size_bytes in sizes.items():
                await uow.backups.update_size(backup_id, size_bytes)
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
    every member must pass the member rule restore and the health probe apply
    (:func:`~mc_server_dashboard_api.archive_member.unrestorable_reason`, issue
    #3579): a regular file or directory at a relative, canonical name, with no
    absolute path, no ``..``, no empty or ``.`` segment and no link or special
    member. Each member is read the way restore reads it, without the tar root
    prefix, so a ``./``-prefixed archive and its ``./`` root entry are accepted.
    The rule runs here so an archive restore would refuse is refused BEFORE it
    lands in the store, rather than failing later as unreadable. A ``..`` between
    backslashes is refused here as well, on top of that rule.

    The compressed body is already capped, but a gzip member can expand ~1000x, so
    the DECOMPRESSED byte count is bounded by ``max_decompressed_bytes`` (#287).
    Every byte decompressed counts (issue #3810): the stream is read through
    :class:`~mc_server_dashboard_api.archive_member.CappedReader`, so headers,
    extended-header data, skipped member data, padding and the read past the
    end-of-archive marker count as member data does, and a walk that seeks back,
    which would decompress the stream again, is refused. So is one over the
    header limits, with the limit as the message: more than 1 MiB of headers
    between two members, since ``tarfile`` holds extended-header data whole, or
    members that hold more than 32 PAX records each or 8 Mi characters of names,
    link names and PAX records in all. ``tarfile`` is opened not to keep the
    members it hands out (``stream=True``, issue #3931). So is a chain of
    extended headers too long for ``tarfile`` to read. Each
    file member's body is drained too, and what it holds counts toward the same
    cap on its own: for a sparse member that includes holes restore writes but
    nothing decompresses.
    Raises :class:`InvalidBackupArchiveError` (422) on any violation.

    The archive must also read to its end (issue #3778). The tar is walked
    through :class:`EndCheckedReader` over :class:`GzipReader` (issue #3835): the
    walk must stop at the end-of-archive marker, so a header past the first that
    does not read is refused rather than cutting the member list short, and the
    stream is then read on to the gzip end. Only zeros may follow the marker,
    each gzip member's header, CRC32 and ISIZE must check, only another gzip
    member or zero padding may follow one, and there may be at most 10,000 of
    them.

    One member shape carries its own error instead (issue #2869): the archive's
    member names ARE the restored working set's rel paths (the packer uses
    ``arcname=child.name``, and :class:`RestoreBackup` republishes the extraction
    as ``current/``), so a member named ``server.properties/…`` would stand a
    DIRECTORY where the platform publishes a file — the #2812 shape, which
    :meth:`RestoreBackup._reapply_platform_properties` is the first thing to fail
    on. It is refused HERE, before the archive is stored, because the extraction
    itself lives in Storage below this seam. The refusal is
    :class:`InvalidFilePathError` with ``reason="platform_managed_path"`` (422) —
    the same answer every files-API door gives, rather than an ``invalid_archive``
    indistinguishable from "not a gzip tar". Only the ROOT name is guarded: a
    nested ``backups/server.properties/…`` member is ordinary user data.
    """

    try:
        with (
            GzipReader(io.BytesIO(content)) as gz,
            tarfile.TarFile(
                fileobj=(
                    stream := EndCheckedReader(
                        CappedReader(gz, max_decompressed_bytes),
                        header_limits=UPLOAD_HEADER_LIMITS,
                    )
                ),
                mode="r",
                stream=True,
            ) as tar,
        ):
            count = 0
            total = 0
            for read in stream.members(tar):
                count += 1
                if count > max_entries:
                    raise InvalidBackupArchiveError("too many entries")
                # Read and judged exactly as restore reads and judges it.
                member = without_tar_root_prefix(read)
                if member is None:
                    continue
                reason = unrestorable_reason(member)
                if reason is not None:
                    raise InvalidBackupArchiveError(
                        f"unsafe member: {_echoed_name(read.name)} {reason}"
                    )
                # Upload-only, on top of the shared rule: ``\`` is read as a
                # separator too, so a ``..`` between backslashes (an archive made
                # on Windows) is refused here. Restore reads ``\`` as an ordinary
                # name character, which is why this is not part of the shared rule.
                if ".." in member.name.replace("\\", "/").split("/"):
                    raise InvalidBackupArchiveError(
                        f"unsafe member: {_echoed_name(read.name)} has a '..' component"
                    )
                # A directory member materializes AT its own name; a file member
                # materializes its PARENT on the way (issue #2869).
                _refuse_dir_at_root_properties(
                    member.name
                    if member.isdir()
                    else str(PurePosixPath(member.name).parent)
                )
                # The Worker's generation-marker name (issue #3433), whether the
                # member is that entry or lies under it.
                _refuse_reserved_root_name(member.name)
                # The member-name rule (issue #3973): restore publishes every
                # member as a name of the working set. Not the characters a
                # files-API door refuses: the platform's own backup of names a
                # running server created must upload again.
                name_refusal = member_name_refusal(member.name, is_dir=member.isdir())
                if name_refusal is not None:
                    raise InvalidBackupArchiveError(
                        f"unsafe member: {_echoed_name(read.name)} is refused by "
                        f"the member-name rule ({name_refusal})"
                    )
                if member.isfile():
                    handle = tar.extractfile(member)
                    if handle is not None:
                        total = _drain_capped(handle, total, max_decompressed_bytes)
            for _chunk in stream.read_past_end_marker():
                pass
    except CapExceededError as exc:
        raise InvalidBackupArchiveError("decompressed size exceeds cap") from exc
    except HeaderLimitError as exc:
        raise InvalidBackupArchiveError(str(exc)) from exc
    except _TAR_GZ_READ_ERRORS as exc:
        # Not ``tarfile.TarError`` alone: a cut or damage inside a member's data
        # surfaces from the gzip layer as ``EOFError`` / ``zlib.error``, and a bad
        # GNU sparse field as a bare ``ValueError`` (#3769). The tuple guards our
        # own code in the loop too, and none of it raises one of these: the
        # helpers it calls (``without_tar_root_prefix``, ``unrestorable_reason``,
        # ``_echoed_name``, the two ``_refuse_*`` guards, ``member_name_refusal``,
        # ``_drain_capped``) only slice, split and build ``PurePosixPath`` s, and
        # refuse with ``ServerError`` s, none of which subclasses a member of the
        # tuple. ``member_name_refusal`` encodes the name too, with
        # ``surrogateescape``, which takes back every byte ``tarfile`` decoded with
        # it, so it raises no ``UnicodeEncodeError`` (a ``ValueError``). The
        # read past the end-of-archive marker raises them by design (#3778): a
        # ``ReadError`` for anything but zeros there, and the gzip layer's own
        # errors for its trailer. So does ``CappedReader`` for a seek back
        # (#3810); the cap is ``CapExceededError``, not one of them, and a walk
        # over the header limits is named above.
        raise InvalidBackupArchiveError("not a gzip tar archive") from exc


def _echoed_name(name: str) -> str:
    """Render an uploaded member name for a refusal message, bounded (issue #3592).

    A name up to :data:`_MAX_ECHOED_NAME_CHARS` renders as its ``repr``; a longer
    one as the ``repr`` of its first that-many characters plus a marker naming the
    original length, so the operator still sees which member it is.
    """

    if len(name) <= _MAX_ECHOED_NAME_CHARS:
        return repr(name)
    return f"{name[:_MAX_ECHOED_NAME_CHARS]!r}…(truncated, {len(name)} chars)"


def _drain_capped(handle: IO[bytes], total: int, max_bytes: int) -> int:
    """Drain a member's body in chunks, bounding the cumulative total.

    Mirrors ``files._read_capped`` (#262) but counts only — the validator never
    needs the bytes, just proof the archive stays under the cap. ``total`` is the
    running sum across prior members; the sum is checked after every chunk so a
    single member aborts mid-read rather than materializing first. The
    ``CappedReader`` beneath already bounds what is decompressed; this bounds what
    a member holds, which for a sparse member includes its holes (issue #3810).
    Returns the updated running total.
    """

    while True:
        chunk = handle.read(_UPLOAD_STREAM_CHUNK)
        if not chunk:
            return total
        total += len(chunk)
        if total > max_bytes:
            raise InvalidBackupArchiveError("decompressed size exceeds cap")
