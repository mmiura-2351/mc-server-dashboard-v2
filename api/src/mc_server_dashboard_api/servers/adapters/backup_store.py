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

from mc_server_dashboard_api.servers.domain.backup_store import BackupArchiveStore
from mc_server_dashboard_api.servers.domain.errors import BackupNotFoundError
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)
from mc_server_dashboard_api.storage.domain.errors import NotFoundError
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
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> str:
        community, server = _scope(community_id, server_id)
        try:
            key = await self._storage.create_backup_from_current(community, server)
        except NotFoundError as exc:
            raise BackupNotFoundError(str(server_id.value)) from exc
        return key.value

    async def restore(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        community, server = _scope(community_id, server_id)
        try:
            await self._storage.restore_backup(
                community, server, BackupKey(storage_ref)
            )
        except NotFoundError as exc:
            raise BackupNotFoundError(storage_ref) from exc

    async def delete(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        community, server = _scope(community_id, server_id)
        # delete_backup is idempotent (STORAGE.md Section 3.3): a missing archive
        # is a no-op, so no NotFoundError translation is needed here.
        await self._storage.delete_backup(community, server, BackupKey(storage_ref))
