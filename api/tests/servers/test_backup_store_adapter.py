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

import errno
import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.domain.backup_store import SnapshotScan
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
from mc_server_dashboard_api.storage.adapters import fs as fs_adapter
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.errors import (
    ArchiveUnreadableError,
    ObjectStoreUnavailableError,
)
from mc_server_dashboard_api.storage.domain.port import ByteStream
from mc_server_dashboard_api.storage.domain.value_objects import (
    BackupKey,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from mc_server_dashboard_api.storage.integrity.region import WorkingSetReport
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory
from tests.storage.helpers import (
    drain,
    healthy_region_bytes,
    mode_invariant_corrupt_region_bytes,
    read_tar,
    region_targz,
    tar_stream,
)


def _ref() -> str:
    """Pre-generate a unique storage ref (mirrors what the application layer does)."""
    return uuid.uuid4().hex


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


# The publish -> backup -> restore round trip is a real-filesystem path whose
# every step fsyncs (atomic snapshot flip, marker rewrite, archive write). That
# makes it legitimately slow under disk contention -- it passes in ~1s isolated
# but has hit the suite-wide 120s pytest-timeout at os.fsync when another full
# suite runs concurrently on the same box (issue #1373). Override the cap for
# just this IO-bound test so disk pressure does not turn a slow-but-correct run
# into a false failure, while still bounding a genuine hang.
@pytest.mark.timeout(300)
async def test_restore_round_trip_recovers_backed_up_content(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()

    # Publish the original working set, then back it up.
    await _publish(storage, community, server, {"server.properties": b"motd=original"})
    ref = _ref()
    await adapter.create_from_current(
        community_id=community, server_id=server, storage_ref=ref
    )

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
        await adapter.create_from_current(
            community_id=community, server_id=server, storage_ref=_ref()
        )


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
    (current / "world" / "region" / "r.0.0.mca").write_bytes(
        mode_invariant_corrupt_region_bytes()
    )

    with pytest.raises(BackupCorruptError):
        await adapter.create_from_current(
            community_id=community, server_id=server, storage_ref=_ref()
        )

    backups = server_root / "backups"
    archives = list(backups.glob("*.tar.gz")) if backups.is_dir() else []
    assert archives == []


class _UnavailableStorage(FsStorage):
    """An ``FsStorage`` whose backup create fails with a storage-backend error.

    Models the object-store adapter's translated ``ObjectStoreUnavailableError`` (the
    2026-07-23 SeaweedFS ``UploadPart`` 500 incident, issue #2270) — a *storage* type
    that the servers seam must not let cross back into the servers layer.
    """

    async def create_backup_from_current(
        self,
        community_id: StorageCommunityId,
        server_id: StorageServerId,
        key: BackupKey | None = None,
    ) -> BackupKey:
        raise ObjectStoreUnavailableError("object store upload failed")


async def test_create_storage_backend_failure_translates_to_backup_storage_unavailable(
    tmp_path: Path,
) -> None:
    """The seam (issue #2270): a storage ``ObjectStoreUnavailableError`` from the
    backup upload is translated to :class:`BackupStorageUnavailableError`, so no raw
    storage type crosses back into the servers layer (the seam's documented contract,
    mirroring the ``IntegrityCheckError`` -> ``BackupCorruptError`` translation)."""

    storage = _UnavailableStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()

    with pytest.raises(BackupStorageUnavailableError):
        await adapter.create_from_current(
            community_id=community, server_id=server, storage_ref=_ref()
        )


class _RestoreUnavailableStorage(FsStorage):
    """An ``FsStorage`` whose restore fails with a storage-backend error (#2273).

    Models the object-client's translated ``ObjectStoreUnavailableError`` reaching the
    restore path (which drives ``upload_multipart`` / single-object writes).
    """

    async def restore_backup(
        self,
        community_id: StorageCommunityId,
        server_id: StorageServerId,
        key: BackupKey,
        *,
        force: bool = False,
    ) -> WorkingSetReport:
        raise ObjectStoreUnavailableError("object store restore failed")


async def test_restore_storage_backend_failure_translates_to_unavailable(
    tmp_path: Path,
) -> None:
    """The seam (issue #2273): a storage ``ObjectStoreUnavailableError`` from restore is
    translated to :class:`BackupStorageUnavailableError`, mirroring create."""

    storage = _RestoreUnavailableStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()

    with pytest.raises(BackupStorageUnavailableError):
        await adapter.restore(
            community_id=community, server_id=server, storage_ref=_ref()
        )


class _StoreUnavailableStorage(FsStorage):
    """An ``FsStorage`` whose put_backup fails with a storage-backend error (#2273)."""

    async def put_backup(
        self,
        community_id: StorageCommunityId,
        server_id: StorageServerId,
        stream: ByteStream,
        key: BackupKey | None = None,
    ) -> BackupKey:
        raise ObjectStoreUnavailableError("object store put_backup failed")


async def test_store_storage_backend_failure_translates_to_unavailable(
    tmp_path: Path,
) -> None:
    """The seam (issue #2273): a storage ``ObjectStoreUnavailableError`` from store is
    translated to :class:`BackupStorageUnavailableError`, mirroring create."""

    storage = _StoreUnavailableStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()

    async def _stream() -> AsyncIterator[bytes]:
        yield b"data"

    with pytest.raises(BackupStorageUnavailableError):
        await adapter.store(
            community_id=community,
            server_id=server,
            stream=_stream(),
            storage_ref=_ref(),
        )


class _PruneUnavailableStorage(FsStorage):
    """An ``FsStorage`` whose prune fails with a storage-backend error (#2273)."""

    async def prune_to_final_snapshot(
        self, community_id: StorageCommunityId, server_id: StorageServerId
    ) -> None:
        raise ObjectStoreUnavailableError("object store prune failed")


async def test_prune_storage_backend_failure_translates_to_unavailable(
    tmp_path: Path,
) -> None:
    """The seam (issue #2273): a storage ``ObjectStoreUnavailableError`` from prune is
    translated to :class:`BackupStorageUnavailableError`, mirroring create."""

    storage = _PruneUnavailableStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()

    with pytest.raises(BackupStorageUnavailableError):
        await adapter.prune_to_final_snapshot(community_id=community, server_id=server)


class _SizeUnavailableStorage(FsStorage):
    """An ``FsStorage`` whose backup_size fails with a storage-backend error (#2378)."""

    async def backup_size(
        self,
        community_id: StorageCommunityId,
        server_id: StorageServerId,
        key: BackupKey,
    ) -> int:
        raise ObjectStoreUnavailableError("object store head failed")


async def test_size_storage_backend_failure_translates_to_unavailable(
    tmp_path: Path,
) -> None:
    """The seam (issue #2378): the size probe backs the download route's declared
    ``Content-Length``, so an outage there must reach the edge as the typed servers
    error the edge maps to 503 — not as a raw storage type routed to a generic 500."""

    storage = _SizeUnavailableStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()

    with pytest.raises(BackupStorageUnavailableError):
        await adapter.size(community_id=community, server_id=server, storage_ref=_ref())


class _DeleteUnavailableStorage(FsStorage):
    """An ``FsStorage`` whose delete_backup fails with a storage-backend error."""

    async def delete_backup(
        self,
        community_id: StorageCommunityId,
        server_id: StorageServerId,
        key: BackupKey,
    ) -> None:
        raise ObjectStoreUnavailableError("object store delete failed")


async def test_delete_storage_backend_failure_translates_to_unavailable(
    tmp_path: Path,
) -> None:
    """The seam (issue #2378): delete_backup drives an object-store delete, so an
    outage there is translated like every other backup write."""

    storage = _DeleteUnavailableStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()

    with pytest.raises(BackupStorageUnavailableError):
        await adapter.delete(
            community_id=community, server_id=server, storage_ref=_ref()
        )


class _ListUnavailableStorage(FsStorage):
    """An ``FsStorage`` whose list_backups fails with a storage-backend error."""

    async def list_backups(
        self, community_id: StorageCommunityId, server_id: StorageServerId
    ) -> list[BackupKey]:
        raise ObjectStoreUnavailableError("object store list failed")


async def test_list_archive_refs_storage_backend_failure_translates_to_unavailable(
    tmp_path: Path,
) -> None:
    """The seam (issue #2378): the delete-server reclaim enumerates archive refs from
    the store between the pack and the row delete, so an outage there must surface as
    the same typed error the pack already raises — one status for one outage."""

    storage = _ListUnavailableStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()

    with pytest.raises(BackupStorageUnavailableError):
        await adapter.list_archive_refs(community_id=community, server_id=server)


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
        storage,
        community,
        server,
        {"world/region/r.0.0.mca": mode_invariant_corrupt_region_bytes()},
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
        storage,
        community,
        server,
        {"world/region/r.0.0.mca": mode_invariant_corrupt_region_bytes()},
    )

    corrupt_count = await adapter.restore(
        community_id=community, server_id=server, storage_ref=ref, force=True
    )

    assert corrupt_count == 1
    # The corrupt backup was published despite the corruption.
    assert (await _hydrate(storage, community, server)) == {
        "world/region/r.0.0.mca": mode_invariant_corrupt_region_bytes()
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
    ref = _ref()
    await adapter.create_from_current(
        community_id=community, server_id=server, storage_ref=ref
    )
    await adapter.delete(community_id=community, server_id=server, storage_ref=ref)
    # A second delete of the same (now-missing) ref is a no-op, not an error.
    await adapter.delete(community_id=community, server_id=server, storage_ref=ref)


async def test_prune_to_final_snapshot_packs_and_drops_working_set(
    tmp_path: Path,
) -> None:
    # The seam delegates to Storage's reclaim (#777): after the prune the working
    # set is gone and a final.tar.gz of it remains at the server root.
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    await _publish(storage, community, server, {"world/level.dat": b"w"})

    await adapter.prune_to_final_snapshot(community_id=community, server_id=server)

    server_root = (
        tmp_path / "communities" / str(community.value) / "servers" / str(server.value)
    )
    final = server_root / "final.tar.gz"
    assert final.is_file()
    assert read_tar(final.read_bytes()) == {"world/level.dat": b"w"}
    assert not (server_root / "current").exists()


async def test_prune_to_final_snapshot_unpublished_is_a_noop(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    # No published snapshot: prune is a no-op rather than an error.
    await adapter.prune_to_final_snapshot(community_id=community, server_id=server)


async def test_open_then_store_to_another_server_restores(tmp_path: Path) -> None:
    """The seam's download (open) + upload (store) round-trip across servers: the
    archive bytes stream out of one server and into another, restorable there."""

    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    await _publish(storage, community, server, {"server.properties": b"motd=original"})
    ref = _ref()
    await adapter.create_from_current(
        community_id=community, server_id=server, storage_ref=ref
    )

    archive = await drain(
        adapter.open(community_id=community, server_id=server, storage_ref=ref)
    )

    other_community, other_server = _scope()
    new_ref = _ref()
    await adapter.store(
        community_id=other_community,
        server_id=other_server,
        stream=_stream_of(archive),
        storage_ref=new_ref,
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


async def test_ranged_open_yields_that_slice_of_the_archive(tmp_path: Path) -> None:
    """The seam passes a byte range through to Storage (issue #2372)."""

    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    await _publish(storage, community, server, {"server.properties": b"motd=original"})
    ref = _ref()
    await adapter.create_from_current(
        community_id=community, server_id=server, storage_ref=ref
    )

    archive = await drain(
        adapter.open(community_id=community, server_id=server, storage_ref=ref)
    )
    tail = await drain(
        adapter.open(
            community_id=community,
            server_id=server,
            storage_ref=ref,
            byte_range=(len(archive) - 10, len(archive) - 1),
        )
    )
    assert tail == archive[-10:]


class _OpenUnavailableStorage(FsStorage):
    """An ``FsStorage`` whose open_backup stream fails with a backend error (#2415).

    Models the object adapter locating the archive: its stream HEADs and GETs the
    object on the first iteration, and the object client translates a backend 5xx
    or a transport failure there into ``ObjectStoreUnavailableError`` (#2376).
    """

    def open_backup(
        self,
        community_id: StorageCommunityId,
        server_id: StorageServerId,
        key: BackupKey,
        *,
        byte_range: tuple[int, int] | None = None,
    ) -> ByteStream:
        async def _gen() -> AsyncIterator[bytes]:
            raise ObjectStoreUnavailableError("object store get failed")
            yield b""  # pragma: no cover - unreachable, keeps this a generator

        return _gen()


async def test_open_storage_backend_failure_translates_to_unavailable(
    tmp_path: Path,
) -> None:
    """The seam (issue #2415): the download route begins the stream before it writes
    the headers, so an outage on the locating half of the read still has a status to
    choose — it must arrive as the servers type the edge maps to 503, not as a raw
    storage type crossing back into the servers layer."""

    storage = _OpenUnavailableStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()

    with pytest.raises(BackupStorageUnavailableError):
        await drain(
            adapter.open(community_id=community, server_id=server, storage_ref=_ref())
        )


async def test_ranged_open_unknown_ref_translates_to_backup_not_found(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    with pytest.raises(BackupNotFoundError):
        await drain(
            adapter.open(
                community_id=community,
                server_id=server,
                storage_ref="nope",
                byte_range=(0, 9),
            )
        )


async def test_size_reports_archive_byte_count(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    await _publish(storage, community, server, {"a": b"1"})
    ref = _ref()
    await adapter.create_from_current(
        community_id=community, server_id=server, storage_ref=ref
    )
    archive = await drain(
        adapter.open(community_id=community, server_id=server, storage_ref=ref)
    )
    size = await adapter.size(community_id=community, server_id=server, storage_ref=ref)
    assert size == len(archive)


async def _stream_of(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def test_check_backup_health_returns_corrupt_count(tmp_path: Path) -> None:
    """The sweep seam (#744): a corrupt archive reports its corrupt-region count."""

    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    good = await _put_backup(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    bad = await _put_backup(
        storage,
        community,
        server,
        {"world/region/r.0.0.mca": mode_invariant_corrupt_region_bytes()},
    )

    assert (
        await adapter.check_backup_health(
            community_id=community, server_id=server, storage_ref=good
        )
        == 0
    )
    assert (
        await adapter.check_backup_health(
            community_id=community, server_id=server, storage_ref=bad
        )
        == 1
    )


class _UnreadableArchiveStorage(FsStorage):
    """A ``Storage`` whose backup readability probe reports the bytes are gone (#2371).

    Models the object adapter's verdict when the stored archive cannot be streamed
    back in full — a *storage* type the servers seam must translate rather than let
    cross back into the servers layer.
    """

    async def check_backup_health(
        self,
        community_id: StorageCommunityId,
        server_id: StorageServerId,
        key: BackupKey,
    ) -> WorkingSetReport:
        raise ArchiveUnreadableError("backup archive unreadable")


async def test_check_backup_health_unreadable_archive_translates_to_unreadable(
    tmp_path: Path,
) -> None:
    """The seam (issue #2371): a storage ``ArchiveUnreadableError`` becomes
    :class:`BackupUnreadableError`, distinct from the corrupt-contents verdict."""

    storage = _UnreadableArchiveStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()

    with pytest.raises(BackupUnreadableError):
        await adapter.check_backup_health(
            community_id=community, server_id=server, storage_ref=_ref()
        )


class _HealthProbeUnavailableStorage(FsStorage):
    """A ``Storage`` whose backup readability probe hits a store outage (#2371)."""

    async def check_backup_health(
        self,
        community_id: StorageCommunityId,
        server_id: StorageServerId,
        key: BackupKey,
    ) -> WorkingSetReport:
        raise ObjectStoreUnavailableError("object store read failed")


async def test_check_backup_health_store_outage_translates_to_unavailable(
    tmp_path: Path,
) -> None:
    """The seam (issue #2371): an outage during the probe is an availability failure,
    NOT a verdict about the archive — it must not reach the sweep as corruption."""

    storage = _HealthProbeUnavailableStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()

    with pytest.raises(BackupStorageUnavailableError):
        await adapter.check_backup_health(
            community_id=community, server_id=server, storage_ref=_ref()
        )


async def test_check_backup_health_unknown_ref_translates_to_backup_not_found(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    with pytest.raises(BackupNotFoundError):
        await adapter.check_backup_health(
            community_id=community, server_id=server, storage_ref="nope"
        )


async def test_check_current_health_returns_corrupt_count(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    await _publish(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    assert (
        await adapter.check_current_health(community_id=community, server_id=server)
        == 0
    )
    # Corrupt the published snapshot in place, then re-check.
    server_root = (
        tmp_path / "communities" / str(community.value) / "servers" / str(server.value)
    )
    current = server_root / os.readlink(server_root / "current")
    (current / "world" / "region" / "r.0.0.mca").write_bytes(
        mode_invariant_corrupt_region_bytes()
    )
    assert (
        await adapter.check_current_health(community_id=community, server_id=server)
        == 1
    )


async def test_check_current_health_unpublished_reports_not_published(
    tmp_path: Path,
) -> None:
    """No published snapshot -> NOT_PUBLISHED, so the sweep skips it (#744)."""

    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    assert (
        await adapter.check_current_health(community_id=community, server_id=server)
        is SnapshotScan.NOT_PUBLISHED
    )


async def test_check_current_health_unexamined_backend_reports_not_examined() -> None:
    """The seam (issue #2377): a backend that examines nothing reports NOT_EXAMINED,
    which the sweep counts apart from an examined-and-clean snapshot.

    Driven against the real object adapter — it has no local working set to walk, so
    it is the backend the distinction exists for.
    """

    storage = ObjectStorage(fake_s3_factory(FakeS3Store()), version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    assert (
        await adapter.check_current_health(community_id=community, server_id=server)
        is SnapshotScan.NOT_EXAMINED
    )


async def test_list_archive_refs_returns_all_filesystem_archives(
    tmp_path: Path,
) -> None:
    """list_archive_refs scans the filesystem, not the DB (#1707)."""

    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    await _publish(storage, community, server, {"a": b"1"})
    ref1 = _ref()
    await adapter.create_from_current(
        community_id=community, server_id=server, storage_ref=ref1
    )
    ref2 = _ref()
    await adapter.create_from_current(
        community_id=community, server_id=server, storage_ref=ref2
    )

    refs = await adapter.list_archive_refs(community_id=community, server_id=server)
    assert set(refs) == {ref1, ref2}


# --- fs I/O-fault parity (issue #2555) ---------------------------------------
#
# The tests above model the object backend by raising ``ObjectStoreUnavailableError``
# from a stubbed public method. These prove the OTHER half of the seam's contract:
# a REAL ``FsStorage`` whose underlying I/O raises a transient errno must produce the
# SAME ``BackupStorageUnavailableError`` the object path does, so both backends answer
# the identical fault with the identical wire response (503 ``storage_unavailable``,
# pinned per route in ``test_backup_endpoints.py``). Before the fs adapter translated
# its errno at the backup boundary, the raw ``OSError`` crossed the seam untranslated
# and every backup route 500'd on the M1 default backend.


def _raise_errno(err: int) -> Callable[..., object]:
    """A drop-in that raises ``OSError(err)`` for any call signature (self absorbed)."""

    def _raise(*args: object, **kwargs: object) -> object:
        raise OSError(err, os.strerror(err))

    return _raise


async def test_create_fs_io_fault_translates_to_backup_storage_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    await _publish(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    monkeypatch.setattr(
        FsStorage, "_write_backup_archive", staticmethod(_raise_errno(errno.EIO))
    )
    with pytest.raises(BackupStorageUnavailableError):
        await adapter.create_from_current(
            community_id=community, server_id=server, storage_ref=_ref()
        )


async def test_restore_fs_io_fault_translates_to_backup_storage_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    ref = await _put_backup(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    monkeypatch.setattr(fs_adapter, "_extract_tar_gz_into", _raise_errno(errno.EIO))
    with pytest.raises(BackupStorageUnavailableError):
        await adapter.restore(community_id=community, server_id=server, storage_ref=ref)


async def test_store_fs_io_fault_translates_to_backup_storage_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    monkeypatch.setattr(tempfile, "mkstemp", _raise_errno(errno.EIO))

    async def _stream() -> AsyncIterator[bytes]:
        yield b"data"

    with pytest.raises(BackupStorageUnavailableError):
        await adapter.store(
            community_id=community,
            server_id=server,
            stream=_stream(),
            storage_ref=_ref(),
        )


async def test_delete_fs_io_fault_translates_to_backup_storage_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    # ``unlink(missing_ok=True)`` swallows ENOENT only; a transient EIO on the unlink
    # is a real fault the delete route must report as 503, not a 500.
    monkeypatch.setattr(os, "unlink", _raise_errno(errno.EIO))
    with pytest.raises(BackupStorageUnavailableError):
        await adapter.delete(
            community_id=community, server_id=server, storage_ref=_ref()
        )


async def test_size_fs_io_fault_translates_to_backup_storage_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    monkeypatch.setattr(fs_adapter, "_size_of_readable", _raise_errno(errno.EIO))
    with pytest.raises(BackupStorageUnavailableError):
        await adapter.size(community_id=community, server_id=server, storage_ref=_ref())


async def test_open_fs_io_fault_translates_to_backup_storage_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The download route's locating half: the stream opens on first iteration, so a
    transient I/O fault there is translated inside the egress generator (#2555)."""

    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    monkeypatch.setattr(fs_adapter, "_open_readable_sync", _raise_errno(errno.EIO))
    with pytest.raises(BackupStorageUnavailableError):
        await drain(
            adapter.open(community_id=community, server_id=server, storage_ref=_ref())
        )


async def test_list_fs_io_fault_translates_to_backup_storage_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing route (#2405): the listing scans the backups dir, so a transient
    I/O fault there must reach the edge as 503, matching its five sibling routes."""

    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    await _publish(storage, community, server, {"a": b"1"})
    await adapter.create_from_current(
        community_id=community, server_id=server, storage_ref=_ref()
    )
    # The backups dir now exists (is_dir passes); fault the iteration itself.
    monkeypatch.setattr(Path, "iterdir", _raise_errno(errno.EIO))
    with pytest.raises(BackupStorageUnavailableError):
        await adapter.list_archive_refs(community_id=community, server_id=server)


async def test_check_backup_health_fs_io_fault_translates_to_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    ref = await _put_backup(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    monkeypatch.setattr(fs_adapter, "_extract_tar_gz_into", _raise_errno(errno.EIO))
    with pytest.raises(BackupStorageUnavailableError):
        await adapter.check_backup_health(
            community_id=community, server_id=server, storage_ref=ref
        )


async def test_prune_fs_io_fault_translates_to_backup_storage_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    await _publish(storage, community, server, {"a": b"1"})
    monkeypatch.setattr(fs_adapter, "_write_tar_gz", _raise_errno(errno.EIO))
    with pytest.raises(BackupStorageUnavailableError):
        await adapter.prune_to_final_snapshot(community_id=community, server_id=server)


async def test_fs_permission_error_stays_a_500_not_a_retryable_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EACCES is a standing misconfiguration, not a transient outage (issue #2555).

    It is deliberately EXCLUDED from the unavailable set: a 503 would invite the
    client to retry a permanently misconfigured directory forever. It must stay a raw
    ``OSError`` (a 500 at the edge), NOT ``BackupStorageUnavailableError`` — proving
    the errno mapping discriminates rather than blanket-translating every ``OSError``.
    """

    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    monkeypatch.setattr(fs_adapter, "_size_of_readable", _raise_errno(errno.EACCES))
    with pytest.raises(OSError) as excinfo:
        await adapter.size(community_id=community, server_id=server, storage_ref=_ref())
    assert excinfo.value.errno == errno.EACCES
    assert not isinstance(excinfo.value, BackupStorageUnavailableError)


@pytest.mark.parametrize(
    "err",
    [
        errno.ETIMEDOUT,
        errno.ENOTCONN,
        errno.EHOSTDOWN,
        errno.EHOSTUNREACH,
        errno.ENETDOWN,
        errno.ENETUNREACH,
    ],
)
async def test_network_transport_errno_translates_to_backup_storage_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: int
) -> None:
    """A remote-fs mount's transport faults are transient outages too (issue #2716).

    ESTALE already translates because a networked mount moved under us; the same
    mount answering with a timed-out or disconnected transport is the identical
    physical fault one step lower in the stack, so it must reach the edge as the
    retryable 503 rather than an opaque 500.
    """

    storage = FsStorage(tmp_path, version_retention=10)
    adapter = StorageBackupStoreAdapter(storage=storage)
    community, server = _scope()
    monkeypatch.setattr(fs_adapter, "_size_of_readable", _raise_errno(err))
    with pytest.raises(BackupStorageUnavailableError):
        await adapter.size(community_id=community, server_id=server, storage_ref=_ref())
