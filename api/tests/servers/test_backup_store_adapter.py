"""Round-trip tests for the servers backup seam against the real ``FsStorage``.

Exercises :class:`StorageBackupStoreAdapter` over a real filesystem ``Storage``
adapter (no DB), proving the FR-BAK-4 atomic-restore round trip end to end:

  publish -> backup -> modify the working set -> restore -> the authoritative copy
  (read back via the hydrate stream) carries the *backed-up* content, not the
  modification.

Also covers create returning an opaque ref, idempotent delete, and the
no-working-set / unknown-ref error translations (storage NotFoundError ->
BackupNotFoundError).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.domain.errors import BackupNotFoundError
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.storage.helpers import drain, read_tar, tar_stream


def _scope() -> tuple[CommunityId, ServerId]:
    return CommunityId(uuid.uuid4()), ServerId(uuid.uuid4())


async def _publish(
    storage: FsStorage,
    community: CommunityId,
    server: ServerId,
    files: dict[str, bytes],
) -> None:
    s_com = StorageCommunityId(community.value)
    s_srv = StorageServerId(server.value)
    handle = await storage.begin_snapshot(s_com, s_srv)
    await storage.write_snapshot(handle, tar_stream(files))
    await storage.commit_snapshot(handle)


async def _hydrate(
    storage: FsStorage, community: CommunityId, server: ServerId
) -> dict[str, bytes]:
    s_com = StorageCommunityId(community.value)
    s_srv = StorageServerId(server.value)
    return read_tar(await drain(storage.open_hydrate_source(s_com, s_srv)))


async def test_restore_round_trip_recovers_backed_up_content(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()

    # Publish the original working set, then back it up.
    await _publish(storage, community, server, {"server.properties": b"motd=original"})
    ref = await adapter.create_from_current(community_id=community, server_id=server)

    # Modify the authoritative copy (a later edit / snapshot).
    await _publish(storage, community, server, {"server.properties": b"motd=changed"})
    assert (await _hydrate(storage, community, server))[
        "server.properties"
    ] == b"motd=changed"

    # Restore the backup; the authoritative copy must carry the backed-up content
    # again, hydrating on the next start with no extra work.
    await adapter.restore(community_id=community, server_id=server, storage_ref=ref)
    assert (await _hydrate(storage, community, server))[
        "server.properties"
    ] == b"motd=original"


async def test_create_with_nothing_published_translates_to_backup_not_found(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    with pytest.raises(BackupNotFoundError):
        await adapter.create_from_current(community_id=community, server_id=server)


async def test_restore_unknown_ref_translates_to_backup_not_found(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    await _publish(storage, community, server, {"a": b"1"})
    with pytest.raises(BackupNotFoundError):
        await adapter.restore(
            community_id=community, server_id=server, storage_ref="nope"
        )


async def test_delete_is_idempotent(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    await _publish(storage, community, server, {"a": b"1"})
    ref = await adapter.create_from_current(community_id=community, server_id=server)
    await adapter.delete(community_id=community, server_id=server, storage_ref=ref)
    # A second delete of the same (now-missing) ref is a no-op, not an error.
    await adapter.delete(community_id=community, server_id=server, storage_ref=ref)
