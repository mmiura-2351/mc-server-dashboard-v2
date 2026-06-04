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

import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    SnapshotServer,
)
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupId,
    BackupSource,
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
    CommandDispatchError,
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
        backup = Backup(
            id=BackupId.new(),
            server_id=server_id,
            storage_ref=storage_ref,
            size_bytes=None,
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
            raise CommandDispatchError(outcome.message or outcome.status.value)
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
