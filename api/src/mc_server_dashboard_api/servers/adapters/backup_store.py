"""Storage-backed adapter for the servers :class:`BackupArchiveStore` seam.

Binds the servers backup seam to the real :class:`Storage` Port (its
``BackupStore`` slice, STORAGE.md Section 3.3). An adapter-layer composition across
bounded contexts (mirroring ``StorageFileStoreAdapter``); the servers *domain* and
*application* never construct a storage adapter — the wiring does.

The seam translates the storage value objects (``BackupKey`` wraps the opaque
archive ref) and the storage ``NotFoundError`` -> :class:`BackupNotFoundError`, so
no storage type crosses back into the servers layer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from mc_server_dashboard_api.servers.domain.backup_store import BackupArchiveStore
from mc_server_dashboard_api.servers.domain.errors import (
    BackupCorruptError,
    BackupNotFoundError,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)
from mc_server_dashboard_api.storage.domain.errors import (
    IntegrityCheckError,
    NotFoundError,
)
from mc_server_dashboard_api.storage.domain.port import Storage
from mc_server_dashboard_api.storage.domain.value_objects import (
    BackupKey,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)


def _scope(
    community_id: CommunityId, server_id: ServerId
) -> tuple[StorageCommunityId, StorageServerId]:
    return (
        StorageCommunityId(community_id.value),
        StorageServerId(server_id.value),
    )


class StorageBackupStoreAdapter(BackupArchiveStore):
    """Bind the servers backup seam to the Storage backup slice."""

    def __init__(self, *, storage: Storage) -> None:
        self._storage = storage

    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        community, server = _scope(community_id, server_id)
        try:
            await self._storage.create_backup_from_current(
                community, server, key=BackupKey(storage_ref)
            )
        except NotFoundError as exc:
            raise BackupNotFoundError(str(server_id.value)) from exc
        except IntegrityCheckError as exc:
            # The working set is structurally corrupt: refuse to archive it (#739).
            # Translate to the servers error so no storage type crosses the seam,
            # carrying the corrupt-file count for the edge log/audit.
            raise BackupCorruptError(
                str(server_id.value), corrupt_count=len(exc.report.corrupt)
            ) from exc

    async def list_archive_refs(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> list[str]:
        community, server = _scope(community_id, server_id)
        keys = await self._storage.list_backups(community, server)
        return [k.value for k in keys]

    async def restore(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        storage_ref: str,
        force: bool = False,
    ) -> int:
        community, server = _scope(community_id, server_id)
        try:
            report = await self._storage.restore_backup(
                community, server, BackupKey(storage_ref), force=force
            )
        except NotFoundError as exc:
            raise BackupNotFoundError(storage_ref) from exc
        except IntegrityCheckError as exc:
            # The extracted backup is structurally corrupt and ``force`` was not
            # set: Storage refused to publish it (#743). Translate to the servers
            # error so no storage type crosses the seam, carrying the corrupt count
            # for the edge log/audit; ``current`` is untouched.
            raise BackupCorruptError(
                storage_ref, corrupt_count=len(exc.report.corrupt)
            ) from exc
        # A forced restore can publish a corrupt working set; return the corrupt
        # region count (0 when healthy) so the use case can quarantine + audit the
        # forced corrupt restore (#743).
        return len(report.corrupt)

    async def check_backup_health(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> int:
        community, server = _scope(community_id, server_id)
        try:
            report = await self._storage.check_backup_health(
                community, server, BackupKey(storage_ref)
            )
        except NotFoundError as exc:
            raise BackupNotFoundError(storage_ref) from exc
        return len(report.corrupt)

    async def check_current_health(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> int | None:
        community, server = _scope(community_id, server_id)
        try:
            report = await self._storage.check_current_health(community, server)
        except NotFoundError:
            # No published snapshot for this server: nothing to fsck. The sweep
            # treats this as "skip" rather than an error (a server may be created
            # but never started/published).
            return None
        return len(report.corrupt)

    async def delete(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        community, server = _scope(community_id, server_id)
        # delete_backup is idempotent (STORAGE.md Section 3.3): a missing archive
        # is a no-op, so no NotFoundError translation is needed here.
        await self._storage.delete_backup(community, server, BackupKey(storage_ref))

    async def prune_to_final_snapshot(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> None:
        community, server = _scope(community_id, server_id)
        # The DeleteServer reclaim path (issue #777): Storage packs ``current/`` into
        # a retained final tar.gz and drops the working-set tree, fail-closed on a
        # pack failure. No NotFoundError translation: an unpublished snapshot is a
        # no-op on Storage, not an error.
        await self._storage.prune_to_final_snapshot(community, server)

    def open(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> AsyncIterator[bytes]:
        community, server = _scope(community_id, server_id)
        # open_backup may raise NotFoundError either at the call (fs) or on first
        # iteration (object); a translating generator catches both into the servers
        # error so no storage type leaks across the seam.
        return self._open(community, server, BackupKey(storage_ref))

    async def _open(
        self,
        community: StorageCommunityId,
        server: StorageServerId,
        key: BackupKey,
    ) -> AsyncIterator[bytes]:
        try:
            stream = self._storage.open_backup(community, server, key)
            async for chunk in stream:
                yield chunk
        except NotFoundError as exc:
            raise BackupNotFoundError(key.value) from exc

    async def store(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        stream: AsyncIterator[bytes],
        storage_ref: str,
    ) -> None:
        community, server = _scope(community_id, server_id)
        await self._storage.put_backup(
            community, server, stream, key=BackupKey(storage_ref)
        )

    async def size(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> int:
        community, server = _scope(community_id, server_id)
        try:
            return await self._storage.backup_size(
                community, server, BackupKey(storage_ref)
            )
        except NotFoundError as exc:
            raise BackupNotFoundError(storage_ref) from exc
