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

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.domain.errors import (
    BackupCorruptError,
    BackupNotFoundError,
)
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
from tests.storage.helpers import (
    corrupt_region_bytes,
    drain,
    healthy_region_bytes,
    read_tar,
    region_targz,
    tar_stream,
)


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


async def test_create_against_corrupt_working_set_raises_and_writes_no_archive(
    tmp_path: Path,
) -> None:
    """The integrity gate (#739): a corrupt ``current/`` refuses the backup create.

    A working set carrying a structurally corrupt ``.mca`` must raise
    :class:`BackupCorruptError` (the seam translation of the storage
    ``IntegrityCheckError``) and write no ``.tar.gz`` archive — a known-corrupt
    world is never archived.
    """

    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    # Publish a healthy snapshot, then corrupt the region file in the live
    # ``current/`` on disk — modelling a crash-corrupted authoritative copy the
    # publish gate could not have caught (a prior-crash truncation, #703).
    await _publish(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    server_root = (
        tmp_path / "communities" / str(community.value) / "servers" / str(server.value)
    )
    current = server_root / os.readlink(server_root / "current")
    (current / "world" / "region" / "r.0.0.mca").write_bytes(corrupt_region_bytes())

    with pytest.raises(BackupCorruptError):
        await adapter.create_from_current(community_id=community, server_id=server)

    backups = server_root / "backups"
    archives = list(backups.glob("*.tar.gz")) if backups.is_dir() else []
    assert archives == []


async def _put_backup(
    storage: FsStorage,
    community: CommunityId,
    server: ServerId,
    files: dict[str, bytes],
) -> str:
    """Store a backup archive of ``files`` verbatim, bypassing the create gate."""

    s_com = StorageCommunityId(community.value)
    s_srv = StorageServerId(server.value)

    async def _stream() -> AsyncIterator[bytes]:
        yield region_targz(files)

    key = await storage.put_backup(s_com, s_srv, _stream())
    return key.value


async def test_restore_corrupt_backup_without_force_translates_to_corrupt_error(
    tmp_path: Path,
) -> None:
    """The restore gate (#743): a corrupt backup without force is BackupCorruptError.

    The seam translates the storage ``IntegrityCheckError`` to
    :class:`BackupCorruptError` (carrying the corrupt count), and ``current`` is
    left resolving to the prior good snapshot — the publish never ran.
    """

    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    await _publish(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    ref = await _put_backup(
        storage, community, server, {"world/region/r.0.0.mca": corrupt_region_bytes()}
    )

    with pytest.raises(BackupCorruptError) as excinfo:
        await adapter.restore(community_id=community, server_id=server, storage_ref=ref)
    assert excinfo.value.corrupt_count == 1
    # current still hydrates the prior healthy region.
    assert (await _hydrate(storage, community, server)) == {
        "world/region/r.0.0.mca": healthy_region_bytes()
    }


async def test_restore_corrupt_backup_with_force_publishes_and_reports_corrupt(
    tmp_path: Path,
) -> None:
    """``force=True`` publishes a corrupt backup, returning the corrupt count (#743)."""

    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    await _publish(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    ref = await _put_backup(
        storage, community, server, {"world/region/r.0.0.mca": corrupt_region_bytes()}
    )

    corrupt_count = await adapter.restore(
        community_id=community, server_id=server, storage_ref=ref, force=True
    )

    assert corrupt_count == 1
    # The corrupt backup was published despite the corruption.
    assert (await _hydrate(storage, community, server)) == {
        "world/region/r.0.0.mca": corrupt_region_bytes()
    }


async def test_restore_healthy_backup_reports_not_corrupt(tmp_path: Path) -> None:
    """A healthy restore returns a zero corrupt count (#743)."""

    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    await _publish(storage, community, server, {"server.properties": b"motd=original"})
    ref = await _put_backup(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )

    corrupt_count = await adapter.restore(
        community_id=community, server_id=server, storage_ref=ref
    )

    assert corrupt_count == 0


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


async def test_open_then_store_to_another_server_restores(tmp_path: Path) -> None:
    """The seam's download (open) + upload (store) round-trip across servers: the
    archive bytes stream out of one server and into another, restorable there."""

    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    await _publish(storage, community, server, {"server.properties": b"motd=original"})
    ref = await adapter.create_from_current(community_id=community, server_id=server)

    archive = await drain(
        adapter.open(community_id=community, server_id=server, storage_ref=ref)
    )

    other_community, other_server = _scope()
    new_ref = await adapter.store(
        community_id=other_community,
        server_id=other_server,
        stream=_stream_of(archive),
    )
    await adapter.restore(
        community_id=other_community, server_id=other_server, storage_ref=new_ref
    )
    assert (await _hydrate(storage, other_community, other_server))[
        "server.properties"
    ] == b"motd=original"


async def test_open_unknown_ref_translates_to_backup_not_found(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    with pytest.raises(BackupNotFoundError):
        await drain(
            adapter.open(community_id=community, server_id=server, storage_ref="nope")
        )


async def test_size_reports_archive_byte_count(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    await _publish(storage, community, server, {"a": b"1"})
    ref = await adapter.create_from_current(community_id=community, server_id=server)
    archive = await drain(
        adapter.open(community_id=community, server_id=server, storage_ref=ref)
    )
    size = await adapter.size(community_id=community, server_id=server, storage_ref=ref)
    assert size == len(archive)


async def _stream_of(data: bytes) -> AsyncIterator[bytes]:
    yield data
