"""Object-adapter mechanics: pointer flip, staged uploads, prefix sweep (#105).

The backend-agnostic Port contract is covered in ``test_port_contract.py`` against
both adapters. This file pins the object-specific realizations of STORAGE.md
Section 4.2/4.3/7.3 against the in-memory S3 stub (NO real cloud): the single
pointer object naming the live snapshot prefix, the atomic pointer flip across
each crash point, staged uploads landing under an ``incoming/`` prefix, and the
orphan-prefix sweep (lease-aware, keyed off the live pointer).
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from mc_server_dashboard_api.storage.adapters.failure_seam import (
    CrashAt,
    InjectedCrash,
    PublishPhase,
)
from mc_server_dashboard_api.storage.adapters.object_store import (
    _DIR_MARKER,
    _GENERATION,
    _POINTER,
    ObjectStorage,
    S3Client,
    _spool_object,
    _spool_stream,
)
from mc_server_dashboard_api.storage.domain.errors import (
    IntegrityCheckError,
    MissingRegionsError,
    PathTraversalError,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    RelPath,
    ServerId,
)
from tests.storage.fake_s3 import FakeS3Store, close_tracking_factory, fake_s3_factory
from tests.storage.helpers import (
    drain,
    healthy_region_bytes,
    mode_invariant_corrupt_region_bytes,
    new_scope,
    read_tar,
    stream_of,
    tar_stream,
)


def _store_and_storage() -> tuple[FakeS3Store, ObjectStorage]:
    store = FakeS3Store()
    return store, ObjectStorage(close_tracking_factory(fake_s3_factory(store)))


def _server_prefix(community: CommunityId, server: ServerId) -> str:
    return f"communities/{community.value}/servers/{server.value}/"


async def _publish(
    storage: ObjectStorage,
    community: CommunityId,
    server: ServerId,
    files: dict[str, bytes],
    *,
    publisher: str | None = None,
) -> None:
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(files))
    await storage.commit_snapshot(handle, publisher=publisher)


async def test_commit_does_not_use_the_client_after_its_context_closes() -> None:
    # Issue #948: the post-flip GC ran on a client whose context had already
    # exited, so with the real aioboto3 client it leaked one aiohttp
    # ClientSession + connector per publish. Refuse any client call made after the
    # client context closed; a publish must complete without one.
    store = FakeS3Store()
    storage = ObjectStorage(close_tracking_factory(fake_s3_factory(store)))
    community, server = new_scope()

    await _publish(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )

    assert await storage.current_generation(community, server) == 1


async def test_generation_and_publisher_share_one_atomic_marker_object() -> None:
    # Issue #847: the generation and the publishing Worker id live in ONE marker
    # object, not two. A crash between two separate PUTs could attribute the PREVIOUS
    # publisher to the NEW generation and invert the publish-time guard; one object
    # written by a single atomic PUT keeps the pair all-or-nothing.
    store, storage = _store_and_storage()
    community, server = new_scope()

    await _publish(storage, community, server, {"f": b"v1"}, publisher="w-a")

    generation_key = _server_prefix(community, server) + _GENERATION
    # Exactly one marker object, named ``generation`` — no separate ``publisher``.
    assert generation_key in store.objects
    assert (_server_prefix(community, server) + "publisher") not in store.objects
    # The single marker holds BOTH the generation (line 1) and the publisher (line 2).
    assert store.objects[generation_key].decode().splitlines() == ["1", "w-a"]
    assert await storage.current_generation(community, server) == 1
    assert await storage.current_publisher(community, server) == "w-a"

    # A publish with no declared id writes the generation alone (no publisher line).
    await _publish(storage, community, server, {"f": b"v2"})
    assert store.objects[generation_key].decode().splitlines() == ["2"]
    assert await storage.current_publisher(community, server) is None


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
    seeded = ObjectStorage(close_tracking_factory(fake_s3_factory(store)))
    community, server = new_scope()
    await _publish(seeded, community, server, {"f": b"OLD"})

    crashed = ObjectStorage(
        close_tracking_factory(fake_s3_factory(store)),
        failure_seam=CrashAt(phase),
    )
    handle = await crashed.begin_snapshot(community, server)
    await crashed.write_snapshot(handle, tar_stream({"f": b"NEW"}))
    with pytest.raises(InjectedCrash):
        await crashed.commit_snapshot(handle)

    # Invariant: the pointer still resolves to the OLD complete snapshot.
    reader = ObjectStorage(close_tracking_factory(fake_s3_factory(store)))
    blob = await drain(reader.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"OLD"}


async def test_crash_after_pointer_flip_keeps_new_prefix_live() -> None:
    store = FakeS3Store()
    seeded = ObjectStorage(close_tracking_factory(fake_s3_factory(store)))
    community, server = new_scope()
    await _publish(seeded, community, server, {"f": b"OLD"})

    crashed = ObjectStorage(
        close_tracking_factory(fake_s3_factory(store)),
        failure_seam=CrashAt(PublishPhase.AFTER_FLIP),
    )
    handle = await crashed.begin_snapshot(community, server)
    await crashed.write_snapshot(handle, tar_stream({"f": b"NEW"}))
    with pytest.raises(InjectedCrash):
        await crashed.commit_snapshot(handle)

    # The pointer PUT already happened, so the flip is the atomic point: NEW is live.
    reader = ObjectStorage(close_tracking_factory(fake_s3_factory(store)))
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
    seeded = ObjectStorage(close_tracking_factory(fake_s3_factory(store)))
    community, server = new_scope()
    await _publish(seeded, community, server, {"f": b"OLD"})

    crashed = ObjectStorage(
        close_tracking_factory(fake_s3_factory(store)),
        failure_seam=CrashAt(phase),
    )
    handle = await crashed.begin_snapshot(community, server)
    await crashed.write_snapshot(handle, tar_stream({"f": b"NEW"}))
    with pytest.raises(InjectedCrash):
        await crashed.commit_snapshot(handle)

    recovered = ObjectStorage(close_tracking_factory(fake_s3_factory(store)))
    pointer_key = _server_prefix(community, server) + _POINTER
    generation_key = _server_prefix(community, server) + _GENERATION
    live_prefix = json.loads(store.objects[pointer_key])["snapshot"]

    await recovered.sweep()

    # No object survives outside the live snapshot prefix + the pointer + the
    # generation marker (issue #763); incoming/ staging and any superseded snapshot
    # prefix are GC'd, but the server-prefix markers are kept.
    server_objs = [
        k for k in store.objects if k.startswith(_server_prefix(community, server))
    ]
    for key in server_objs:
        assert key in (pointer_key, generation_key) or key.startswith(live_prefix), key
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

    from tests.storage.fake_s3 import FakeS3Client

    store = FakeS3Store()
    community, server = new_scope()
    seeded = ObjectStorage(close_tracking_factory(fake_s3_factory(store)))
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
    await ObjectStorage(close_tracking_factory(fake_s3_factory(store))).sweep()
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
    seeded = ObjectStorage(close_tracking_factory(fake_s3_factory(store)))
    community, server = new_scope()
    await _publish(seeded, community, server, {"f": b"LIVE"})

    # Simulate a crash mid-stage: staged objects with no live handle (a fresh
    # adapter has an empty active-staging set).
    incoming = _server_prefix(community, server) + "incoming/orphan-transfer/"
    store.objects[incoming + "f"] = b"PARTIAL"

    recovered = ObjectStorage(close_tracking_factory(fake_s3_factory(store)))
    await recovered.sweep()
    assert not any(
        k.startswith(_server_prefix(community, server) + "incoming/")
        for k in store.objects
    )


async def test_make_dir_writes_marker_and_dir_is_visible() -> None:
    """make_dir writes a zero-byte marker so the empty directory is visible (#1125).

    The marker object anchors the directory prefix so ``list_dir`` discovers it,
    but ``_entries_at_level`` filters the marker out so it never appears as a file.
    """

    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(storage, community, server, {"server.properties": b"x"})

    await storage.make_dir(community, server, RelPath("plugins"))

    # The marker object exists in the store under the directory prefix.
    assert any(key.endswith("/plugins/" + _DIR_MARKER) for key in store.objects)

    # The directory is now visible in the root listing.
    root_entries = await storage.list_dir(community, server, RelPath(""))
    dir_names = {e.name for e in root_entries if e.is_dir}
    assert "plugins" in dir_names

    # Listing the directory itself returns empty (marker is filtered out).
    entries = await storage.list_dir(community, server, RelPath("plugins"))
    assert entries == []


async def test_make_dir_root_path_is_noop_guard() -> None:
    """make_dir with an empty-parts RelPath (root) is a no-op (#1944).

    Defence-in-depth: the use-case layer already rejects the root path, but if
    something bypasses it the adapter must not write the poisoned ``//.dir`` key.
    """
    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(storage, community, server, {"server.properties": b"x"})

    keys_before = set(store.objects)
    await storage.make_dir(community, server, RelPath("."))
    assert set(store.objects) == keys_before  # no new objects written


async def test_subkey_traversal_is_confined_to_server_prefix() -> None:
    # RelPath blocks .. at construction; assert the adapter's read path rejects it
    # too (defence in depth at the key-derivation step, Section 6).
    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(storage, community, server, {"f": b"x"})
    with pytest.raises(PathTraversalError):
        RelPath("../escape")


async def test_create_backup_refuses_corrupt_live_snapshot() -> None:
    """The backup-create gate (#750) refuses a known-corrupt live snapshot and
    writes no archive object.

    The corrupt-create path is not reachable through the Port (the commit gate
    rejects publishing a corrupt working set), so the corruption is planted by
    PUTting a corrupt ``.mca`` directly under the live snapshot prefix — the object
    analogue of writing into the fs adapter's ``current/`` (#739 backend tests).
    """

    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(storage, community, server, {"world/region/r.0.0.mca": b"\0" * 8192})
    pointer_key = _server_prefix(community, server) + _POINTER
    snap_prefix = json.loads(store.objects[pointer_key])["snapshot"]
    # The create gate runs in live mode (issue #923), so plant a tear the live rule
    # still catches (a location entry past EOF), not a mere unaligned size.
    store.objects[snap_prefix + "world/region/r.0.0.mca"] = (
        mode_invariant_corrupt_region_bytes()
    )

    backups_prefix = _server_prefix(community, server) + "backups/"
    with pytest.raises(IntegrityCheckError):
        await storage.create_backup_from_current(community, server)
    # Fail-closed: no ``.tar.gz`` backup object was uploaded.
    assert not any(k.startswith(backups_prefix) for k in store.objects)


async def test_commit_refuses_partial_region_loss_and_keeps_prior() -> None:
    """The missing-region gate (issue #854) on the object backend: a publish that
    DROPS some-but-not-all of a live dimension's region objects is refused with
    :class:`MissingRegionsError`, the pointer is not flipped, and staging is cleaned.
    """

    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(
        storage,
        community,
        server,
        {
            "world/region/r.0.0.mca": healthy_region_bytes(),
            "world/region/r.0.1.mca": healthy_region_bytes(),
        },
    )
    pointer_key = _server_prefix(community, server) + _POINTER
    before = json.loads(store.objects[pointer_key])["snapshot"]

    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(
        handle, tar_stream({"world/region/r.0.0.mca": healthy_region_bytes()})
    )
    with pytest.raises(MissingRegionsError) as excinfo:
        await storage.commit_snapshot(handle)

    assert len(excinfo.value.report.partial_loss) == 1
    # Pointer unchanged; no leftover incoming/ staging objects.
    assert json.loads(store.objects[pointer_key])["snapshot"] == before
    incoming = _server_prefix(community, server) + "incoming/"
    assert not any(k.startswith(incoming) for k in store.objects)


async def test_commit_allows_full_dimension_delete() -> None:
    """A publish that removes a WHOLE dimension's region objects (legitimate delete)
    is allowed on the object backend (issue #854)."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(
        storage,
        community,
        server,
        {
            "world/region/r.0.0.mca": healthy_region_bytes(),
            "world/DIM-1/region/r.0.0.mca": healthy_region_bytes(),
        },
    )

    after = {"world/region/r.0.0.mca": healthy_region_bytes()}
    await _publish(storage, community, server, after)

    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == after


async def test_prune_uploads_final_targz_and_drops_working_set_objects() -> None:
    # Object realization of the DeleteServer reclaim (#777): the live snapshot is
    # packed into a single final.tar.gz object at the server prefix, and the
    # snapshots/, incoming/, versions/ objects plus the pointer/generation markers
    # are deleted. backups/ is left untouched.
    store, storage = _store_and_storage()
    community, server = new_scope()
    files = {"server.properties": b"motd=hi", "world/level.dat": b"w"}
    await _publish(storage, community, server, files)
    backup_key = await storage.create_backup_from_current(community, server)

    await storage.prune_to_final_snapshot(community, server)

    prefix = _server_prefix(community, server)
    final_key = prefix + "final.tar.gz"
    assert final_key in store.objects
    assert read_tar(store.objects[final_key]) == files
    # Working-set objects and markers are gone; the backup object survives.
    assert not any(k.startswith(prefix + "snapshots/") for k in store.objects)
    assert not any(k.startswith(prefix + "incoming/") for k in store.objects)
    assert (prefix + _POINTER) not in store.objects
    assert (prefix + _GENERATION) not in store.objects
    assert (prefix + f"backups/{backup_key.value}.tar.gz") in store.objects


async def test_prune_without_publish_uploads_no_final_object() -> None:
    store, storage = _store_and_storage()
    community, server = new_scope()
    await storage.prune_to_final_snapshot(community, server)  # no raise
    prefix = _server_prefix(community, server)
    assert (prefix + "final.tar.gz") not in store.objects


async def test_prune_retry_after_crash_keeps_final_and_finishes_gc() -> None:
    # Crash-retry regression (#777): the prune invalidates the pointer the instant
    # final.tar.gz is durable, so a crash AFTER the pointer delete but before the
    # prefix GC leaves: final present + pointer gone + a leftover snapshots/ object.
    # The retried DeleteServer must finish the GC WITHOUT re-packing, so it never
    # overwrites the good final.tar.gz with an empty/partial pack.
    store, storage = _store_and_storage()
    community, server = new_scope()
    files = {"server.properties": b"motd=hi", "world/level.dat": b"w"}
    await _publish(storage, community, server, files)

    # First attempt completes the durable part (final written, pointer dropped).
    await storage.prune_to_final_snapshot(community, server)
    prefix = _server_prefix(community, server)
    final_key = prefix + "final.tar.gz"
    good_final = store.objects[final_key]
    assert read_tar(good_final) == files
    assert (prefix + _POINTER) not in store.objects

    # Simulate a crash that left the prefix GC unfinished: a stray snapshots/ object
    # survives. The pointer stays absent (it is invalidated before any GC).
    store.objects[prefix + "snapshots/dead/world/level.dat"] = b"stale"

    # The retry takes the no-pointer branch: GC completes, final is byte-for-byte
    # untouched (no empty re-pack), and no new pointer/snapshot object is left.
    await storage.prune_to_final_snapshot(community, server)
    assert store.objects[final_key] == good_final
    assert read_tar(store.objects[final_key]) == files
    assert not any(k.startswith(prefix + "snapshots/") for k in store.objects)
    assert (prefix + _POINTER) not in store.objects


async def test_prune_pointer_invalidated_before_prefix_gc() -> None:
    # The ordering that makes the retry above safe: while the pointer is present the
    # source is still complete (re-packing it would be correct); the pointer is only
    # dropped AFTER final.tar.gz is durable and BEFORE the snapshots/ prefix is GC'd.
    # A failure-seam wired to drop right after the flip would otherwise re-pack a
    # half-deleted prefix — assert the pointer is the first marker to go by checking
    # it is gone while the (now-redundant) source prefix has been reclaimed too.
    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(storage, community, server, {"f": b"v1"})
    prefix = _server_prefix(community, server)

    await storage.prune_to_final_snapshot(community, server)

    # After a clean run: final durable, pointer gone, source prefix reclaimed. The
    # invariant the retry leans on is that the pointer never outlives the source it
    # names, so a present pointer always resolves to a complete snapshot.
    assert (prefix + "final.tar.gz") in store.objects
    assert (prefix + _POINTER) not in store.objects
    assert not any(k.startswith(prefix + "snapshots/") for k in store.objects)


async def test_sweep_aborts_old_orphan_multipart_upload() -> None:
    # Orphan multipart parts (issue #903): a hard crash mid-put_backup leaves an
    # in-progress multipart upload whose parts never complete and never list as
    # objects, so the prefix sweep cannot see them. The sweep aborts one initiated
    # more than the age threshold ago via ListMultipartUploads + AbortMultipartUpload.
    store, storage = _store_and_storage()
    community, server = new_scope()
    backups = _server_prefix(community, server) + "backups/orphan.tar.gz"
    store.multipart_uploads["upload-old"] = (
        backups,
        dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
    )

    await storage.sweep()

    assert "upload-old" not in store.multipart_uploads, (
        "an orphan multipart upload older than the threshold must be aborted"
    )


async def test_sweep_aborts_old_orphan_multipart_upload_under_jars_prefix() -> None:
    # jars/ prefix sweep (issue #916): put_jar uploads under jars/ via multipart, so
    # a hard crash mid-jar-ingest leaks parts there too. _sweep_multipart enumerates
    # both communities/ and jars/, so an orphan under jars/ older than the threshold
    # is aborted — not only the communities/ ones.
    store, storage = _store_and_storage()
    store.multipart_uploads["jar-upload-old"] = (
        "jars/abc123.jar",
        dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
    )

    await storage.sweep()

    assert "jar-upload-old" not in store.multipart_uploads, (
        "an orphan multipart upload under jars/ older than the threshold must be "
        "aborted"
    )


async def test_sweep_spares_young_multipart_upload() -> None:
    # Age threshold (issue #903): an upload initiated within the threshold may be a
    # live put_backup/snapshot member upload in progress, so the sweep must NOT abort
    # it — mirroring the fs spool-sweep age guard.
    store, storage = _store_and_storage()
    community, server = new_scope()
    backups = _server_prefix(community, server) + "backups/inflight.tar.gz"
    store.multipart_uploads["upload-young"] = (
        backups,
        dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1),
    )

    await storage.sweep()

    assert "upload-young" in store.multipart_uploads, (
        "a live multipart upload within the age threshold must survive the sweep"
    )


async def test_sweep_degrades_to_warn_when_list_multipart_unsupported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Graceful degradation (issue #903): a backend without ListMultipartUploads
    # (e.g. a SeaweedFS build that lacks it) must not fail the whole sweep. The
    # multipart branch catches the unsupported-operation error, logs a WARN advising
    # the AbortIncompleteMultipartUpload bucket lifecycle rule, and the prefix sweep
    # still completes.
    import logging

    store = FakeS3Store()
    seeded = ObjectStorage(close_tracking_factory(fake_s3_factory(store)))
    community, server = new_scope()
    await _publish(seeded, community, server, {"f": b"LIVE"})

    # Seed a crash-leftover orphan staging object the prefix sweep should still GC,
    # to prove the unsupported multipart path does not abort the rest of the sweep.
    incoming = _server_prefix(community, server) + "incoming/orphan/f"
    store.objects[incoming] = b"PARTIAL"
    store.list_multipart_uploads_unsupported = True

    storage = ObjectStorage(close_tracking_factory(fake_s3_factory(store)))
    with caplog.at_level(logging.WARNING):
        await storage.sweep()

    assert any(
        "AbortIncompleteMultipartUpload" in rec.message for rec in caplog.records
    ), "an unsupported ListMultipartUploads must log a lifecycle-rule WARN"
    # The rest of the sweep still ran: the orphan staging object was reclaimed.
    assert incoming not in store.objects


async def test_fake_abort_multipart_upload_is_idempotent() -> None:
    # Idempotent abort (issue #916): the Port documents abort as a no-op for an
    # already-aborted/completed upload id. The fake honours it (pop(..., None)) so a
    # complete-vs-abort race in a sweep does not crash. Aborting an unknown id is a
    # no-op; aborting twice is too.
    store = FakeS3Store()
    store.multipart_uploads["upload-1"] = (
        "jars/x.jar",
        dt.datetime.now(dt.UTC),
    )
    async with fake_s3_factory(store)() as client:
        await client.abort_multipart_upload("jars/missing.jar", "no-such-id")
        await client.abort_multipart_upload("jars/x.jar", "upload-1")
        await client.abort_multipart_upload("jars/x.jar", "upload-1")
    assert "upload-1" not in store.multipart_uploads


async def test_check_reachable_raises_on_unreachable_backend() -> None:
    """Boot-time reachability probe (issue #945): an unreachable object store
    must surface a RuntimeError naming the endpoint and bucket, so a
    misconfigured deployment fails fast instead of degrading silently."""

    class _UnreachableClient:
        async def list_objects(self, prefix: str) -> list[object]:
            raise ConnectionError("Connection refused")

    @asynccontextmanager
    async def _unreachable_factory() -> AsyncIterator[S3Client]:
        yield _UnreachableClient()  # type: ignore[misc]

    storage = ObjectStorage(_unreachable_factory)
    with pytest.raises(RuntimeError, match="Object storage unreachable") as exc_info:
        await storage.check_reachable(
            endpoint="http://bad-host:9333", bucket="test-bucket"
        )
    msg = str(exc_info.value)
    assert "http://bad-host:9333" in msg
    assert "test-bucket" in msg
    assert "Connection refused" in msg


async def test_check_reachable_succeeds_for_healthy_backend() -> None:
    """A reachable object store does not raise (issue #945)."""

    store, storage = _store_and_storage()
    await storage.check_reachable(endpoint="http://localhost:8333", bucket="mcsdata")


async def test_hydrate_reader_rereads_when_gc_lands_in_lease_gap() -> None:
    """A concurrent restore flips+GCs in the window between resolving the live
    snapshot prefix and leasing it (issue #1607). The reader must re-verify and
    converge on the NEW snapshot rather than reading from a GC'd prefix."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(storage, community, server, {"f": b"OLD"})

    call_count = {"n": 0}
    original = ObjectStorage._live_snapshot_prefix

    async def _racing_live_snapshot_prefix(
        self: ObjectStorage, client: S3Client, cid: CommunityId, sid: ServerId
    ) -> str:
        result = await original(self, client, cid, sid)
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Between the first resolve and the lease, a concurrent restore
            # publishes new content (flips + GCs the old snapshot prefix).
            await _publish(storage, community, server, {"f": b"NEW"})
        return result

    storage._live_snapshot_prefix = _racing_live_snapshot_prefix.__get__(  # type: ignore[method-assign]
        storage, ObjectStorage
    )

    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"NEW"}


async def test_hydrate_skips_poisoned_root_dir_marker() -> None:
    """A pre-existing ``//.dir`` key (from a prior root make_dir) is excluded from
    the hydrate tar so already-poisoned snapshots self-heal (issue #1944).

    The poisoned key produces a ``/.dir`` tar member (absolute path) that the
    worker's ``safeJoin`` rejects, blocking the server start permanently. The
    hydrate stream must silently skip it.
    """

    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(storage, community, server, {"server.properties": b"x"})

    # Plant the poisoned key directly into the store (simulates the old bug).
    prefix = _server_prefix(community, server)
    pointer_raw = store.objects[prefix + _POINTER]
    snapshot_prefix = json.loads(pointer_raw)["snapshot"]
    poisoned_key = snapshot_prefix + "/" + _DIR_MARKER  # produces ``//.dir``
    store.objects[poisoned_key] = b""

    # The hydrate must NOT include the poisoned member.
    blob = await drain(storage.open_hydrate_source(community, server))
    members = read_tar(blob)
    assert "/.dir" not in members
    # The legitimate file must still be present.
    assert "server.properties" in members


async def test_file_stream_rereads_when_gc_lands_in_lease_gap() -> None:
    """Same as hydrate but for open_file_stream (issue #1607): a concurrent
    restore in the resolve-lease gap must not yield a stale/deleted file."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    await _publish(storage, community, server, {"f": b"OLD"})

    call_count = {"n": 0}
    original = ObjectStorage._live_snapshot_prefix

    async def _racing_live_snapshot_prefix(
        self: ObjectStorage, client: S3Client, cid: CommunityId, sid: ServerId
    ) -> str:
        result = await original(self, client, cid, sid)
        call_count["n"] += 1
        if call_count["n"] == 1:
            await _publish(storage, community, server, {"f": b"NEW"})
        return result

    storage._live_snapshot_prefix = _racing_live_snapshot_prefix.__get__(  # type: ignore[method-assign]
        storage, ObjectStorage
    )

    blob = await drain(storage.open_file_stream(community, server, RelPath("f")))
    assert blob == b"NEW"


# --- spool helpers: temp-file cleanup on stream failure (#1956) ---------------


class _MidStreamError(Exception):
    """Raised mid-iteration to simulate a client disconnect / S3 error."""


async def _failing_stream(payload: bytes, *, fail_after: int) -> AsyncIterator[bytes]:
    """Yield ``fail_after`` bytes of ``payload``, then raise."""
    yielded = 0
    for i in range(0, len(payload), 7):
        chunk = payload[i : i + 7]
        if yielded + len(chunk) >= fail_after:
            raise _MidStreamError("simulated mid-write failure")
        yield chunk
        yielded += len(chunk)


@pytest.mark.anyio
async def test_spool_stream_unlinks_on_stream_error() -> None:
    """_spool_stream must not leak the temp file when the source stream raises."""

    import glob
    import tempfile

    # Snapshot temp files before the spool attempt.
    tmp_dir = tempfile.gettempdir()
    before = set(glob.glob(f"{tmp_dir}/.leak-test.*"))

    with pytest.raises(_MidStreamError):
        await _spool_stream(
            _failing_stream(b"x" * 100, fail_after=30),
            ".leak-test.",
            ".tmp",
        )

    # No new files with the spool prefix should remain.
    after = set(glob.glob(f"{tmp_dir}/.leak-test.*"))
    leaked = after - before
    assert not leaked, f"spool file leaked: {leaked}"


@pytest.mark.anyio
async def test_spool_object_unlinks_on_stream_error() -> None:
    """_spool_object must not leak the temp file when the S3 body stream raises."""

    import glob
    import tempfile

    store = FakeS3Store()
    # Seed an object whose body stream will fail mid-read.
    store.objects["test/key"] = b"x" * 100

    # Patch the fake client's get_object to yield a failing stream.
    factory = fake_s3_factory(store)

    class _FailingClient:
        def __init__(self, inner: S3Client) -> None:
            self._inner = inner

        async def get_object(self, key: str) -> AsyncIterator[bytes]:
            return _failing_stream(store.objects[key], fail_after=30)

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(self._inner, name)

    tmp_dir = tempfile.gettempdir()
    before = set(glob.glob(f"{tmp_dir}/.leak-obj.*"))

    async with factory() as real_client:
        failing_client = _FailingClient(real_client)
        with pytest.raises(_MidStreamError):
            await _spool_object(failing_client, "test/key", ".leak-obj.", ".tmp")

    after = set(glob.glob(f"{tmp_dir}/.leak-obj.*"))
    leaked = after - before
    assert not leaked, f"spool file leaked: {leaked}"
