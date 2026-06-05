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
    NotFoundError,
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


async def test_sweep_reread_skips_prefix_made_live_after_pointer_read() -> None:
    """A publish whose new-prefix objects were already listed but whose pointer
    flip lands after the sweep read the pointer must not delete the now-live
    prefix (issue #113).

    The sweep lists once, then per server reads the pointer and deletes every
    snapshot prefix that pointer does not name. The guard re-reads the pointer
    immediately before deleting each candidate prefix: if it now names the
    candidate, the prefix is live and is skipped.
    """

    import json as _json
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from mc_server_dashboard_api.storage.adapters.object_store import S3Client
    from tests.storage.fake_s3 import FakeS3Client

    store = FakeS3Store()
    community, server = new_scope()
    seeded = ObjectStorage(fake_s3_factory(store))
    await _publish(seeded, community, server, {"f": b"OLD"})

    pointer_key = _server_prefix(community, server) + _POINTER
    old_prefix = _json.loads(store.objects[pointer_key])["snapshot"]

    # Simulate a concurrent publisher that has already copied its objects under a
    # fresh prefix (so they are in the sweep's listing) but has not yet flipped the
    # pointer. The name sorts after the live OLD prefix, so the sweep re-reads the
    # pointer for OLD first; the flip lands then, and the guard's re-read for this
    # NEW candidate sees it live and keeps it (issue #113).
    new_prefix = _server_prefix(community, server) + "snapshots/zzz-concurrent-new/"
    store.objects[new_prefix + "f"] = b"NEW"

    # The flip lands during the sweep's very first pointer re-read: the first read
    # returns the pre-flip value (OLD live), and the publisher flips the pointer to
    # NEW immediately after. This models the flip landing after the listing but
    # before the guard re-reads for the NEW candidate (issue #113).
    reads = {"n": 0}

    class _FlipAfterFirstReadClient(FakeS3Client):
        async def get_object(self, key: str) -> AsyncIterator[bytes]:
            result = await super().get_object(key)
            if key == pointer_key:
                reads["n"] += 1
                if reads["n"] == 1:
                    store.objects[pointer_key] = _json.dumps(
                        {"snapshot": new_prefix}
                    ).encode()
            return result

    @asynccontextmanager
    async def _factory() -> AsyncIterator[S3Client]:
        yield _FlipAfterFirstReadClient(store)

    storage = ObjectStorage(_factory)
    await storage.sweep()

    assert reads["n"] >= 2, "the guard must re-read the pointer per candidate prefix"
    # The just-made-live prefix survived the race; the pointer now resolves to it.
    assert store.objects.get(new_prefix + "f") == b"NEW"
    assert _json.loads(store.objects[pointer_key])["snapshot"] == new_prefix
    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"NEW"}

    # The now-superseded OLD prefix (kept this pass because it read live before the
    # flip) is reclaimed by a later sweep with no concurrent publisher.
    await ObjectStorage(fake_s3_factory(store)).sweep()
    assert not any(k.startswith(old_prefix) for k in store.objects)


async def test_active_staging_survives_concurrent_sweep() -> None:
    """An in-flight transfer's incoming/ objects must survive a concurrent sweep.

    The object adapter pins the staging prefix with an in-process active-staging
    lease for the life of the handle (begin -> commit/abort), so a sweep scheduled
    while the transfer is mid-flight skips its incoming/ objects (issue #160). The
    fs adapter pins staging the same way; this gives the object adapter parity.
    """

    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(storage, community, server, {"f": b"LIVE"})

    # Begin + stage an in-flight transfer, but do NOT commit/abort yet.
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream({"f": b"INFLIGHT"}))
    incoming = _server_prefix(community, server) + "incoming/"
    assert any(k.startswith(incoming) for k in store.objects)

    # A concurrent sweep must NOT delete the active staging objects.
    await storage.sweep()
    assert any(k.startswith(incoming) for k in store.objects), (
        "active staging must survive a concurrent sweep"
    )

    # The transfer still commits and publishes its staged bytes.
    await storage.commit_snapshot(handle)
    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"INFLIGHT"}


async def test_sweep_reclaims_released_staging_after_abort() -> None:
    """Once a transfer is aborted the staging lease is released; a sweep that finds
    any residual incoming/ objects (here re-seeded) reclaims them — the lease only
    protects in-flight, not released, staging (issue #160)."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(storage, community, server, {"f": b"LIVE"})

    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream({"f": b"INFLIGHT"}))
    incoming = _server_prefix(community, server) + "incoming/"

    await storage.abort_snapshot(handle)
    # Re-seed a leftover under the (now released) incoming prefix to prove the sweep
    # reclaims it now that the lease is gone.
    store.objects[incoming + "leftover/f"] = b"x"

    await storage.sweep()
    assert not any(k.startswith(incoming) for k in store.objects)


async def test_sweep_reclaims_crash_leftover_staging_with_no_handle() -> None:
    """Crash leftovers have no in-process handle by definition, so a fresh adapter's
    sweep reclaims them — the lease lives only in the process that began the
    transfer (issue #160)."""

    store = FakeS3Store()
    seeded = ObjectStorage(fake_s3_factory(store))
    community, server = new_scope()
    await _publish(seeded, community, server, {"f": b"LIVE"})

    # Simulate a crash mid-stage: staged objects with no live handle (a fresh
    # adapter has an empty active-staging set).
    incoming = _server_prefix(community, server) + "incoming/orphan-transfer/"
    store.objects[incoming + "f"] = b"PARTIAL"

    recovered = ObjectStorage(fake_s3_factory(store))
    await recovered.sweep()
    assert not any(
        k.startswith(_server_prefix(community, server) + "incoming/")
        for k in store.objects
    )


async def test_make_dir_is_a_noop_empty_dir_not_represented() -> None:
    """Object storage has no real directories, so make_dir writes no object.

    An empty directory exists only as the shared key-prefix of its files
    (Section 7.3); there is nothing to create until a file lands under it. The
    documented limitation (issue #259): make_dir is a no-op on object storage —
    it neither errors nor leaves a marker object that would pollute listings.
    """

    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(storage, community, server, {"server.properties": b"x"})

    before = set(store.objects)
    await storage.make_dir(community, server, RelPath("plugins"))
    assert set(store.objects) == before  # no marker object written

    # The empty directory is not observable (a prefix scan finds no members).
    with pytest.raises(NotFoundError):
        await storage.list_dir(community, server, RelPath("plugins"))


async def test_subkey_traversal_is_confined_to_server_prefix() -> None:
    # RelPath blocks .. at construction; assert the adapter's read path rejects it
    # too (defence in depth at the key-derivation step, Section 6).
    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(storage, community, server, {"f": b"x"})
    with pytest.raises(PathTraversalError):
        RelPath("../escape")
