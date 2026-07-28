"""Storage-backed adapter for the servers :class:`BackupArchiveStore` seam.

Binds the servers backup seam to the real :class:`Storage` Port (its
``BackupStore`` slice, STORAGE.md Section 3.3). An adapter-layer composition across
bounded contexts (mirroring ``StorageFileStoreAdapter``); the servers *domain* and
*application* never construct a storage adapter — the wiring does.

The seam translates the storage value objects (``BackupKey`` wraps the opaque
archive ref) and the storage errors so no storage type crosses back into the
servers layer: ``NotFoundError`` -> :class:`BackupNotFoundError`,
``IntegrityCheckError`` -> :class:`BackupCorruptError`,
``ObjectStoreUnavailableError`` -> :class:`BackupStorageUnavailableError` (#2270),
and ``ArchiveUnreadableError`` -> :class:`BackupUnreadableError` (#2371).

The ``ObjectStoreUnavailableError`` translation covers every method that reaches the
store *before* a response body starts, so one outage yields one status at the edge
(503 ``storage_unavailable``, issue #2378). The lone exception is :meth:`open`: the
stream's failure surfaces after the headers are on the wire, where no status is left
to choose, so it stays a truncated body guarded by the route's byte count (#2318).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from mc_server_dashboard_api.servers.domain.backup_store import BackupArchiveStore
from mc_server_dashboard_api.servers.domain.errors import (
    BackupCorruptError,
    BackupNotFoundError,
    BackupStorageUnavailableError,
    BackupUnreadableError,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)
from mc_server_dashboard_api.storage.domain.errors import (
    ArchiveUnreadableError,
    IntegrityCheckError,
    NotFoundError,
    ObjectStoreUnavailableError,
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
        except ObjectStoreUnavailableError as exc:
            # The object store surfaced a transport/backend failure during the archive
            # upload (issue #2270): a botocore ClientError (e.g. the SeaweedFS 500
            # InternalError on UploadPart) or a connection/timeout, already translated
            # to a storage type at the object-client boundary. Translate again here so
            # no storage type crosses the seam back into the servers layer.
            raise BackupStorageUnavailableError(str(server_id.value)) from exc

    async def list_archive_refs(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> list[str]:
        community, server = _scope(community_id, server_id)
        try:
            keys = await self._storage.list_backups(community, server)
        except ObjectStoreUnavailableError as exc:
            # The delete-server reclaim enumerates archive refs between the pack and
            # the row delete (issue #2378). Without this the same outage that makes
            # the pack raise the typed error leaks a raw storage type here, so one
            # DELETE would surface as 503 or 500 depending on which call lost.
            raise BackupStorageUnavailableError(str(server_id.value)) from exc
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
        except ObjectStoreUnavailableError as exc:
            # The object store surfaced a transport/backend failure during the restore
            # (issue #2273): already translated to a storage type at the object-client
            # boundary. Translate again here so no storage type crosses the seam back
            # into the servers layer, mirroring create_from_current.
            raise BackupStorageUnavailableError(str(server_id.value)) from exc
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
        except ArchiveUnreadableError as exc:
            # The object backend's readability probe could not stream the stored
            # archive back in full (issue #2371) — a verdict about the archive's
            # BYTES, which has no ``WorkingSetReport`` to report a corrupt count
            # from. Translate to its own servers error so the sweep can quarantine
            # it and say why, without a storage type crossing the seam.
            raise BackupUnreadableError(storage_ref) from exc
        except ObjectStoreUnavailableError as exc:
            # The probe reads every archive byte, so a backend outage lands here
            # readily (issue #2371). It is NOT a verdict about the archive: keeping
            # it a distinct servers error is what stops the sweep from quarantining
            # every backup in the deployment over one bad minute.
            raise BackupStorageUnavailableError(storage_ref) from exc
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
        try:
            await self._storage.delete_backup(community, server, BackupKey(storage_ref))
        except ObjectStoreUnavailableError as exc:
            # A store outage is not the idempotent missing-archive case: the archive
            # may still be there. Translate at the seam (issue #2378) so the edge
            # reports the same transient 503 it reports for every other backup write.
            raise BackupStorageUnavailableError(str(server_id.value)) from exc

    async def prune_to_final_snapshot(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> None:
        community, server = _scope(community_id, server_id)
        # The DeleteServer reclaim path (issue #777): Storage packs ``current/`` into
        # a retained final tar.gz and drops the working-set tree, fail-closed on a
        # pack failure. No NotFoundError translation: an unpublished snapshot is a
        # no-op on Storage, not an error.
        try:
            await self._storage.prune_to_final_snapshot(community, server)
        except ObjectStoreUnavailableError as exc:
            # The pack drives object-store writes (upload_multipart / delete_object): a
            # transport/backend failure surfaces as a storage type (issue #2273).
            # Translate at the seam so no storage type crosses back into the servers
            # layer, mirroring create_from_current.
            raise BackupStorageUnavailableError(str(server_id.value)) from exc

    def open(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        storage_ref: str,
        byte_range: tuple[int, int] | None = None,
    ) -> AsyncIterator[bytes]:
        community, server = _scope(community_id, server_id)
        # open_backup raises NotFoundError on the stream's first iteration — both
        # backends locate the archive as they open it (issue #2341) — so the
        # translation into the servers error wraps the iteration, not the call.
        return self._open(community, server, BackupKey(storage_ref), byte_range)

    async def _open(
        self,
        community: StorageCommunityId,
        server: StorageServerId,
        key: BackupKey,
        byte_range: tuple[int, int] | None,
    ) -> AsyncIterator[bytes]:
        try:
            stream = self._storage.open_backup(
                community, server, key, byte_range=byte_range
            )
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
        try:
            await self._storage.put_backup(
                community, server, stream, key=BackupKey(storage_ref)
            )
        except ObjectStoreUnavailableError as exc:
            # put_backup drives an object-store upload_multipart: a transport/backend
            # failure surfaces as a storage type (issue #2273). Translate at the seam
            # so no storage type crosses back into the servers layer, mirroring
            # create_from_current.
            raise BackupStorageUnavailableError(str(server_id.value)) from exc

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
        except ObjectStoreUnavailableError as exc:
            # The size probe backs the download route's declared Content-Length and
            # runs before any byte is on the wire (issue #2378), so translating here
            # is what lets that route answer 503 instead of a generic 500.
            raise BackupStorageUnavailableError(str(server_id.value)) from exc
