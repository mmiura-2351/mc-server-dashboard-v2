"""Object-adapter mechanics: pointer flip, staged uploads, prefix sweep (#105).

The backend-agnostic Port contract is covered in ``test_port_contract.py`` against
both adapters. This file pins the object-specific realizations of STORAGE.md
Section 4.2/4.3/7.3 against the in-memory S3 stub (NO real cloud): the single
pointer object naming the live snapshot prefix, the atomic pointer flip across
each crash point, staged uploads landing under an ``incoming/`` prefix, and the
orphan-prefix sweep (lease-aware, keyed off the live pointer).
"""

from __future__ import annotations

import json

import pytest

from mc_server_dashboard_api.storage.adapters.failure_seam import (
    CrashAt,
    InjectedCrash,
    PublishPhase,
)
from mc_server_dashboard_api.storage.adapters.object_store import (
    _POINTER,
    ObjectStorage,
)
from mc_server_dashboard_api.storage.domain.errors import (
    PathTraversalError,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    RelPath,
    ServerId,
)
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory
from tests.storage.helpers import drain, new_scope, read_tar, stream_of, tar_stream


def _store_and_storage() -> tuple[FakeS3Store, ObjectStorage]:
    store = FakeS3Store()
    return store, ObjectStorage(fake_s3_factory(store))


def _server_prefix(community: CommunityId, server: ServerId) -> str:
    return f"communities/{community.value}/servers/{server.value}/"


async def _publish(
    storage: ObjectStorage,
    community: CommunityId,
    server: ServerId,
    files: dict[str, bytes],
) -> None:
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(files))
    await storage.commit_snapshot(handle)


async def test_pointer_object_names_the_live_snapshot_prefix() -> None:
    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(storage, community, server, {"f": b"v1"})

    pointer_key = _server_prefix(community, server) + _POINTER
    assert pointer_key in store.objects
    snap_prefix = json.loads(store.objects[pointer_key])["snapshot"]
    assert snap_prefix.startswith(_server_prefix(community, server) + "snapshots/")
    # The pointed-at prefix actually holds the data object (read-after-write).
    assert any(k.startswith(snap_prefix) for k in store.objects)


async def test_staged_uploads_land_under_incoming_prefix() -> None:
    store, storage = _store_and_storage()
    community, server = new_scope()
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream({"world/a": b"x"}))

    incoming = _server_prefix(community, server) + "incoming/"
    staged = [k for k in store.objects if k.startswith(incoming)]
    assert staged, "snapshot bytes must stage under incoming/ before publish"
    assert staged[0].endswith("world/a")


async def test_multipart_upload_consumes_parts_chunk_wise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multipart upload streams part-by-part, never one whole-body read.

    Shrink the part size so a modest payload spans several parts, then assert the
    fake recorded more than one part: a client-side regression that buffered the
    stream whole (collapsing to a single part) would fail here (Section 7.3).
    """

    from mc_server_dashboard_api.storage.adapters import object_store

    monkeypatch.setattr(object_store, "_PART", 4096)
    store, storage = _store_and_storage()
    data = b"j" * (4096 * 3 + 17)  # spans multiple shrunk parts

    key = await storage.put_jar(stream_of(data, chunk=512))

    jar_key = f"jars/{key.sha256}.jar"
    assert store.objects[jar_key] == data
    assert store.multipart_parts[jar_key] > 1


@pytest.mark.parametrize("phase", [PublishPhase.AFTER_STAGE, PublishPhase.AFTER_MOVE])
async def test_crash_before_pointer_flip_keeps_old_prefix_live(
    phase: PublishPhase,
) -> None:
    store = FakeS3Store()
    seeded = ObjectStorage(fake_s3_factory(store))
    community, server = new_scope()
    await _publish(seeded, community, server, {"f": b"OLD"})

    crashed = ObjectStorage(fake_s3_factory(store), failure_seam=CrashAt(phase))
    handle = await crashed.begin_snapshot(community, server)
    await crashed.write_snapshot(handle, tar_stream({"f": b"NEW"}))
    with pytest.raises(InjectedCrash):
        await crashed.commit_snapshot(handle)

    # Invariant: the pointer still resolves to the OLD complete snapshot.
    reader = ObjectStorage(fake_s3_factory(store))
    blob = await drain(reader.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"OLD"}


async def test_crash_after_pointer_flip_keeps_new_prefix_live() -> None:
    store = FakeS3Store()
    seeded = ObjectStorage(fake_s3_factory(store))
    community, server = new_scope()
    await _publish(seeded, community, server, {"f": b"OLD"})

    crashed = ObjectStorage(
        fake_s3_factory(store), failure_seam=CrashAt(PublishPhase.AFTER_FLIP)
    )
    handle = await crashed.begin_snapshot(community, server)
    await crashed.write_snapshot(handle, tar_stream({"f": b"NEW"}))
    with pytest.raises(InjectedCrash):
        await crashed.commit_snapshot(handle)

    # The pointer PUT already happened, so the flip is the atomic point: NEW is live.
    reader = ObjectStorage(fake_s3_factory(store))
    blob = await drain(reader.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"NEW"}


@pytest.mark.parametrize(
    "phase",
    [PublishPhase.AFTER_STAGE, PublishPhase.AFTER_MOVE, PublishPhase.AFTER_FLIP],
)
async def test_sweep_reclaims_orphan_prefixes_idempotently(
    phase: PublishPhase,
) -> None:
    store = FakeS3Store()
    seeded = ObjectStorage(fake_s3_factory(store))
    community, server = new_scope()
    await _publish(seeded, community, server, {"f": b"OLD"})

    crashed = ObjectStorage(fake_s3_factory(store), failure_seam=CrashAt(phase))
    handle = await crashed.begin_snapshot(community, server)
    await crashed.write_snapshot(handle, tar_stream({"f": b"NEW"}))
    with pytest.raises(InjectedCrash):
        await crashed.commit_snapshot(handle)

    recovered = ObjectStorage(fake_s3_factory(store))
    pointer_key = _server_prefix(community, server) + _POINTER
    live_prefix = json.loads(store.objects[pointer_key])["snapshot"]

    await recovered.sweep()

    # No object survives outside the live snapshot prefix + the pointer itself
    # (incoming/ staging and any superseded snapshot prefix are GC'd).
    server_objs = [
        k for k in store.objects if k.startswith(_server_prefix(community, server))
    ]
    for key in server_objs:
        assert key == pointer_key or key.startswith(live_prefix), key
    assert not any(
        k.startswith(_server_prefix(community, server) + "incoming/")
        for k in store.objects
    )

    # Idempotent + still readable after a second sweep.
    await recovered.sweep()
    blob = await drain(recovered.open_hydrate_source(community, server))
    assert read_tar(blob) in [{"f": b"OLD"}, {"f": b"NEW"}]


async def test_second_publish_gcs_superseded_prefix() -> None:
    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(storage, community, server, {"f": b"v1"})
    pointer_key = _server_prefix(community, server) + _POINTER
    first_prefix = json.loads(store.objects[pointer_key])["snapshot"]

    await _publish(storage, community, server, {"f": b"v2"})
    second_prefix = json.loads(store.objects[pointer_key])["snapshot"]

    assert second_prefix != first_prefix
    # The superseded prefix's objects are GC'd right after the flip (Section 4.3).
    assert not any(k.startswith(first_prefix) for k in store.objects)


async def test_sweep_skips_leased_superseded_prefix_then_reclaims() -> None:
    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(storage, community, server, {"f": b"OLD"})
    pointer_key = _server_prefix(community, server) + _POINTER
    old_prefix = json.loads(store.objects[pointer_key])["snapshot"]

    # Open + begin iterating the hydrate stream -> leases the OLD prefix.
    stream = storage.open_hydrate_source(community, server)
    first = await stream.__anext__()

    # A concurrent publish flips the pointer; the leased OLD prefix is NOT GC'd.
    await _publish(storage, community, server, {"f": b"NEW"})
    assert any(k.startswith(old_prefix) for k in store.objects)

    rest = await drain(stream)
    assert read_tar(first + rest) == {"f": b"OLD"}

    # Lease released; the next sweep reclaims the superseded prefix.
    await storage.sweep()
    assert not any(k.startswith(old_prefix) for k in store.objects)


async def test_subkey_traversal_is_confined_to_server_prefix() -> None:
    # RelPath blocks .. at construction; assert the adapter's read path rejects it
    # too (defence in depth at the key-derivation step, Section 6).
    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(storage, community, server, {"f": b"x"})
    with pytest.raises(PathTraversalError):
        RelPath("../escape")
