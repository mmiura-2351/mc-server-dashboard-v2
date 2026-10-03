"""A body that dies mid-transfer, followed through every servers seam (issue #2380).

The object client translates ANY failure while reading a response body into
``ObjectStoreUnavailableError`` (``_iter_body``, issue #2375) — the same type a
store outage at request initiation already raised (#2376). Every object read
therefore surfaces a mid-body failure as that type, not only the backup
readability probe it was written for. These tests drive the real
:class:`ObjectStorage` over the in-memory S3 stub, tear one object's body down
partway (``FakeS3Store.read_aborts``), and pin what each servers seam that reads
it hands back — the type the route above it maps to its edge status.

The property each test guards is that the failure is reported as a store outage
and nothing else: not a miss (a 404 / a silently skipped member), not a corrupt
world, and not a short body passed off as a complete one. Where the seam has a
modelled outage type (backups, resource packs) it must arrive as that type,
which the routes answer 503 ``storage_unavailable``. Where it has none (files,
the plugin cache) the storage type still crosses the seam and the edge answers
the generic 500 — the same answer an outage at request initiation gets there.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.file_store import (
    StorageFileStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.plugin_cache_store import (
    ObjectPluginCacheStore,
)
from mc_server_dashboard_api.servers.adapters.resource_pack_store import (
    ObjectResourcePackStore,
)
from mc_server_dashboard_api.servers.domain.errors import (
    BackupStorageUnavailableError,
    ResourcePackStorageUnavailableError,
)
from mc_server_dashboard_api.servers.domain.resource_pack import ResourcePackId
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.errors import (
    ObjectStoreUnavailableError,
    StorageUnavailableError,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.storage.fake_s3 import FakeS3Store, close_tracking_factory, fake_s3_factory
from tests.storage.helpers import drain, healthy_region_bytes, tar_stream

# Large enough that an abort partway through leaves bytes on both sides of it.
_CONTENT = bytes(range(256)) * 16
_ABORT_AT = 1000


def _object_storage() -> tuple[ObjectStorage, FakeS3Store]:
    backing = FakeS3Store()
    storage = ObjectStorage(close_tracking_factory(fake_s3_factory(backing)))
    return storage, backing


def _scope() -> tuple[CommunityId, ServerId]:
    return CommunityId(uuid.uuid4()), ServerId(uuid.uuid4())


def _storage_scope(
    community: CommunityId, server: ServerId
) -> tuple[StorageCommunityId, StorageServerId]:
    return StorageCommunityId(community.value), StorageServerId(server.value)


def _server_prefix(community: CommunityId, server: ServerId) -> str:
    return f"communities/{community.value}/servers/{server.value}/"


async def _publish(
    storage: ObjectStorage,
    community: CommunityId,
    server: ServerId,
    files: dict[str, bytes],
) -> None:
    handle = await storage.begin_snapshot(*_storage_scope(community, server))
    await storage.write_snapshot(handle, tar_stream(files))
    await storage.commit_snapshot(handle)


def _only_key(backing: FakeS3Store, *, under: str, ending: str) -> str:
    """The single stored key under the ``under`` segment that ends with ``ending``."""

    keys = [k for k in backing.objects if f"/{under}/" in k and k.endswith(ending)]
    assert len(keys) == 1, keys
    return keys[0]


async def _delivered_before(
    stream: AsyncIterator[bytes], error: type[BaseException]
) -> bytes:
    """The bytes a stream yielded before it raised ``error`` (which it must)."""

    got = bytearray()
    with pytest.raises(error):
        async for chunk in stream:
            got += chunk
    return bytes(got)


# --- files: no modelled outage, so the edge answers 500 ---------------------


async def test_file_read_mid_body_failure_is_an_outage_not_a_miss() -> None:
    """``GET .../files?path=`` at rest: the read must not come back as the
    ``ServerFileNotFoundError`` its route answers 404 for — a client told "not
    found" would never retry a file that is still there."""

    storage, backing = _object_storage()
    community, server = _scope()
    await _publish(storage, community, server, {"server.properties": _CONTENT})
    key = _only_key(backing, under="snapshots", ending="/server.properties")
    backing.read_aborts[key] = [_ABORT_AT]
    adapter = StorageFileStoreAdapter(storage=storage)

    with pytest.raises(StorageUnavailableError):
        await adapter.read_file(
            community_id=community, server_id=server, rel_path="server.properties"
        )


async def test_file_stream_mid_body_failure_raises_after_the_delivered_bytes() -> None:
    """The single-file download streams after its headers are written, so a body
    that dies partway must end the stream with an error — never a clean end of
    stream that would pass a short file off as the whole one."""

    storage, backing = _object_storage()
    community, server = _scope()
    await _publish(storage, community, server, {"level.dat": _CONTENT})
    key = _only_key(backing, under="snapshots", ending="/level.dat")
    backing.read_aborts[key] = [_ABORT_AT]
    adapter = StorageFileStoreAdapter(storage=storage)

    delivered = await _delivered_before(
        adapter.open_file_stream(
            community_id=community, server_id=server, rel_path="level.dat"
        ),
        StorageUnavailableError,
    )

    assert delivered == _CONTENT[:_ABORT_AT]


@pytest.mark.parametrize("abort_at", [0, _ABORT_AT])
async def test_dir_zip_member_failure_aborts_the_zip_instead_of_skipping_it(
    abort_at: int,
) -> None:
    """The directory download (and the server export, which shares the stream)
    skips a member that vanished or is refused. A member whose body the store
    could not deliver is neither: skipping it would finish a well-formed zip that
    is silently missing a file. Offset 0 is the case that matters most — the
    failure lands on the very first pull, where the skip decision is made."""

    storage, backing = _object_storage()
    community, server = _scope()
    await _publish(
        storage,
        community,
        server,
        {"world/level.dat": _CONTENT, "world/session.lock": b"lock"},
    )
    key = _only_key(backing, under="snapshots", ending="/world/level.dat")
    backing.read_aborts[key] = [abort_at]
    adapter = StorageFileStoreAdapter(storage=storage)

    with pytest.raises(StorageUnavailableError):
        await drain(
            adapter.download_dir(
                community_id=community, server_id=server, rel_path="world"
            )
        )


async def test_file_version_read_mid_body_failure_is_an_outage_not_a_miss() -> None:
    """``GET .../files/version``: same rule as the live read — the 404 its route
    gives a missing version is not the answer for a version that is there."""

    storage, backing = _object_storage()
    community, server = _scope()
    await _publish(storage, community, server, {"server.properties": _CONTENT})
    adapter = StorageFileStoreAdapter(storage=storage)
    await adapter.write_file(
        community_id=community,
        server_id=server,
        rel_path="server.properties",
        content=b"motd=new",
    )
    [version_id] = await adapter.list_versions(
        community_id=community, server_id=server, rel_path="server.properties"
    )
    key = _only_key(backing, under="versions", ending=f"/{version_id}")
    backing.read_aborts[key] = [_ABORT_AT]

    with pytest.raises(StorageUnavailableError):
        await adapter.read_version(
            community_id=community,
            server_id=server,
            rel_path="server.properties",
            version_id=version_id,
        )


# --- backups: the modelled outage, which every backup route answers 503 -----


async def _backup(
    storage: ObjectStorage, community: CommunityId, server: ServerId
) -> tuple[StorageBackupStoreAdapter, str]:
    await _publish(storage, community, server, {"server.properties": _CONTENT})
    adapter = StorageBackupStoreAdapter(storage=storage)
    ref = uuid.uuid4().hex
    await adapter.create_from_current(
        community_id=community, server_id=server, storage_ref=ref
    )
    return adapter, ref


def _archive_key(community: CommunityId, server: ServerId, ref: str) -> str:
    return _server_prefix(community, server) + f"backups/{ref}.tar.gz"


async def test_backup_restore_mid_body_failure_is_unavailable_and_keeps_current() -> (
    None
):
    """``POST .../backups/{id}/restore``: the archive spool dies partway. That is
    an outage (503), not the ``BackupCorruptError`` a damaged world gets (500
    ``working_set_corrupt``, which also quarantines the backup) — and the restore
    must publish nothing."""

    storage, backing = _object_storage()
    community, server = _scope()
    adapter, ref = await _backup(storage, community, server)
    pointer_key = _server_prefix(community, server) + "current.json"
    pointer_before = backing.objects[pointer_key]
    # Derived from the compressed archive, which is far smaller than _CONTENT: a
    # fixed offset past its end would deliver it whole before failing.
    archive = backing.objects[_archive_key(community, server, ref)]
    backing.read_aborts[_archive_key(community, server, ref)] = [len(archive) // 2]

    with pytest.raises(BackupStorageUnavailableError):
        await adapter.restore(community_id=community, server_id=server, storage_ref=ref)

    assert backing.objects[pointer_key] == pointer_before
    assert not [k for k in backing.objects if "/incoming/" in k]


@pytest.mark.parametrize(
    "rel_path",
    [
        # Read by the archive builder.
        "server.properties",
        # Read first by the integrity gate, which must not call it corrupt.
        "world/region/r.0.0.mca",
    ],
)
async def test_backup_create_mid_body_failure_is_unavailable_and_writes_nothing(
    rel_path: str,
) -> None:
    """``POST .../backups``: a working-set object dies partway while the archive
    is built. An outage (503), not a corrupt world (500 ``working_set_corrupt``),
    and no archive object is left behind to list as a backup."""

    storage, backing = _object_storage()
    community, server = _scope()
    await _publish(
        storage,
        community,
        server,
        {
            "server.properties": _CONTENT,
            "world/region/r.0.0.mca": healthy_region_bytes(),
        },
    )
    key = _only_key(backing, under="snapshots", ending=f"/{rel_path}")
    backing.read_aborts[key] = [_ABORT_AT]
    adapter = StorageBackupStoreAdapter(storage=storage)

    with pytest.raises(BackupStorageUnavailableError):
        await adapter.create_from_current(
            community_id=community, server_id=server, storage_ref=uuid.uuid4().hex
        )

    assert not [k for k in backing.objects if "/backups/" in k]


async def test_backup_download_failure_at_the_first_byte_is_unavailable() -> None:
    """The download route pulls the first chunk before it writes its headers
    (issue #2415), so a body that dies before its first byte still reaches the
    route as the type it answers 503 for."""

    storage, backing = _object_storage()
    community, server = _scope()
    adapter, ref = await _backup(storage, community, server)
    backing.read_aborts[_archive_key(community, server, ref)] = [0]

    with pytest.raises(BackupStorageUnavailableError):
        await drain(
            adapter.open(community_id=community, server_id=server, storage_ref=ref)
        )


async def test_backup_download_mid_body_failure_raises_after_the_delivered_bytes() -> (
    None
):
    """Past the first chunk the status is committed: the stream must still end in
    an error (the route's byte count then aborts the response, #2318), never a
    clean end that passes a truncated archive off as complete."""

    storage, backing = _object_storage()
    community, server = _scope()
    adapter, ref = await _backup(storage, community, server)
    archive = backing.objects[_archive_key(community, server, ref)]
    abort_at = len(archive) // 2
    backing.read_aborts[_archive_key(community, server, ref)] = [abort_at]

    delivered = await _delivered_before(
        adapter.open(community_id=community, server_id=server, storage_ref=ref),
        BackupStorageUnavailableError,
    )

    assert delivered == archive[:abort_at]


async def test_server_delete_pack_mid_body_failure_is_unavailable_and_keeps_data() -> (
    None
):
    """``DELETE .../servers/{id}`` packs the working set into a final archive
    first. A body dying partway is an outage (503) and the delete stays
    fail-closed: the working set and its pointer are left in place."""

    storage, backing = _object_storage()
    community, server = _scope()
    await _publish(storage, community, server, {"server.properties": _CONTENT})
    key = _only_key(backing, under="snapshots", ending="/server.properties")
    backing.read_aborts[key] = [_ABORT_AT]
    adapter = StorageBackupStoreAdapter(storage=storage)

    with pytest.raises(BackupStorageUnavailableError):
        await adapter.prune_to_final_snapshot(community_id=community, server_id=server)

    prefix = _server_prefix(community, server)
    assert prefix + "current.json" in backing.objects
    assert key in backing.objects
    assert prefix + "final.tar.gz" not in backing.objects


# --- resource packs: the modelled outage, answered 503 before the body ------


async def test_resource_pack_mid_body_failure_raises_after_the_delivered_bytes() -> (
    None
):
    """Both pack download routes begin the stream before their headers, so a body
    that dies at its first byte is their 503 (pinned with the store's contract).
    Past that point the stream must end in the same servers error — the routes'
    byte count turns it into an aborted response (#2337) — and not a clean end a
    game client would record as a complete pack."""

    backing = FakeS3Store()
    store = ObjectResourcePackStore(close_tracking_factory(fake_s3_factory(backing)))
    pack_id = ResourcePackId(uuid.uuid4())

    async def _blob() -> AsyncIterator[bytes]:
        yield _CONTENT

    await store.put(pack_id, "pack.zip", _blob())
    backing.read_aborts[f"resource-packs/{pack_id.value}/pack.zip"] = [_ABORT_AT]

    delivered = await _delivered_before(
        store.open(pack_id, "pack.zip"), ResourcePackStorageUnavailableError
    )

    assert delivered == _CONTENT[:_ABORT_AT]


# --- plugin cache: no modelled outage, so the edge answers 500 --------------


async def test_plugin_cache_mid_body_failure_is_an_outage_not_a_cache_miss() -> None:
    """The plugin cache seam translates only a missing blob
    (``PluginCacheBlobNotFoundError``, which the catalog resolver treats as a
    miss and downloads instead). A blob whose body dies partway is not missing:
    it surfaces as the store outage, and the callers buffer the whole blob before
    using it, so no truncated jar is ever written."""

    backing = FakeS3Store()
    cache = ObjectPluginCacheStore(close_tracking_factory(fake_s3_factory(backing)))
    sha256 = "a" * 64

    async def _blob() -> AsyncIterator[bytes]:
        yield _CONTENT

    await cache.put(sha256, _blob())
    backing.read_aborts[f"plugin-cache/{sha256}"] = [_ABORT_AT]

    delivered = await _delivered_before(cache.open(sha256), ObjectStoreUnavailableError)

    assert delivered == _CONTENT[:_ABORT_AT]
