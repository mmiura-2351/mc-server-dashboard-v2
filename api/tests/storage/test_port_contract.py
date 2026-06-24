"""Backend-agnostic Storage Port contract, parametrized over fs + object (#105).

These assertions depend only on the Port surface and its observable guarantees
(STORAGE.md Sections 3, 4) — never on a backend's internal layout — so they run
unchanged against both adapters via the ``harness`` fixture (conftest). Backend
mechanics (fs symlinks / crash phases; object pointer flip / prefix sweep) are
covered in the per-adapter files.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.errors import (
    ArchiveTooLargeError,
    IncompleteTransferError,
    IntegrityCheckError,
    NotFoundError,
    PathTraversalError,
    SnapshotHandleError,
    StaleGenerationError,
)
from mc_server_dashboard_api.storage.domain.port import (
    API_EDIT_PUBLISHER,
    RESTORE_PUBLISHER,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    BackupKey,
    CommunityId,
    JarKey,
    RelPath,
    ServerId,
)
from tests.storage.conftest import StorageHarness, build_harness
from tests.storage.helpers import (
    bomb_targz,
    corrupt_region_bytes,
    drain,
    healthy_region_bytes,
    malicious_tar_with_escape,
    mode_invariant_corrupt_region_bytes,
    new_scope,
    read_tar,
    region_targz,
    stream_of,
    tar_bytes,
    tar_stream,
    unaligned_live_region_bytes,
)

# --- working-set hydrate / snapshot (Section 3.1) --------------------------


async def test_commit_then_hydrate_round_trips(harness: StorageHarness) -> None:
    community, server = new_scope()
    files = {"server.properties": b"a=b", "world/level.dat": b"world-bytes"}
    await harness.publish(community, server, files)

    blob = await drain(harness.storage.open_hydrate_source(community, server))
    assert read_tar(blob) == files


async def test_hydrate_before_any_publish_is_not_found(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    with pytest.raises(NotFoundError):
        await drain(harness.storage.open_hydrate_source(community, server))


async def test_second_publish_supersedes(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"v1"})
    await harness.publish(community, server, {"f": b"v2"})

    blob = await drain(harness.storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"v2"}


async def test_commit_bumps_generation_and_round_trips(
    harness: StorageHarness,
) -> None:
    # Each commit_snapshot bumps the per-server working-set generation, returns the
    # new value, and current_generation reads it back (issue #763). A never-published
    # server is generation 0, matching the Worker's "nothing held" default.
    community, server = new_scope()
    assert await harness.storage.current_generation(community, server) == 0

    handle = await harness.storage.begin_snapshot(community, server)
    await harness.storage.write_snapshot(handle, tar_stream({"f": b"v1"}))
    first = await harness.storage.commit_snapshot(handle)
    assert first == 1
    assert await harness.storage.current_generation(community, server) == 1

    handle = await harness.storage.begin_snapshot(community, server)
    await harness.storage.write_snapshot(handle, tar_stream({"f": b"v2"}))
    second = await harness.storage.commit_snapshot(handle)
    assert second == 2
    assert await harness.storage.current_generation(community, server) == 2


async def test_commit_records_publisher_and_reads_it_back(
    harness: StorageHarness,
) -> None:
    # commit_snapshot records the publishing Worker id alongside the generation and
    # current_publisher reads it back (issue #847 bug 3). A never-published server has
    # no publisher; a publish with no declared id leaves it None (a permissive guard).
    community, server = new_scope()
    assert await harness.storage.current_publisher(community, server) is None

    handle = await harness.storage.begin_snapshot(community, server)
    await harness.storage.write_snapshot(handle, tar_stream({"f": b"v1"}))
    await harness.storage.commit_snapshot(handle, publisher="worker-a")
    assert await harness.storage.current_publisher(community, server) == "worker-a"

    # A later publish overwrites the recorded publisher with the new producer.
    handle = await harness.storage.begin_snapshot(community, server)
    await harness.storage.write_snapshot(handle, tar_stream({"f": b"v2"}))
    await harness.storage.commit_snapshot(handle, publisher="worker-b")
    assert await harness.storage.current_publisher(community, server) == "worker-b"

    # A publish declaring no id clears the marker back to None.
    handle = await harness.storage.begin_snapshot(community, server)
    await harness.storage.write_snapshot(handle, tar_stream({"f": b"v3"}))
    await harness.storage.commit_snapshot(handle)
    assert await harness.storage.current_publisher(community, server) is None


async def test_generation_is_per_server(harness: StorageHarness) -> None:
    # The generation counter is scoped per server, not global (issue #763).
    community, server_a = new_scope()
    _, server_b = new_scope()

    handle = await harness.storage.begin_snapshot(community, server_a)
    await harness.storage.write_snapshot(handle, tar_stream({"f": b"a"}))
    assert await harness.storage.commit_snapshot(handle) == 1

    handle = await harness.storage.begin_snapshot(community, server_b)
    await harness.storage.write_snapshot(handle, tar_stream({"f": b"b"}))
    assert await harness.storage.commit_snapshot(handle) == 1
    assert await harness.storage.current_generation(community, server_a) == 1


async def test_refused_commit_does_not_bump_generation(
    harness: StorageHarness,
) -> None:
    # An integrity-refused publish does NOT bump the generation (issue #763): the
    # generation tracks only successfully published working sets.
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"good"})
    assert await harness.storage.current_generation(community, server) == 1

    handle = await harness.storage.begin_snapshot(community, server)
    await harness.storage.write_snapshot(
        handle, tar_stream({"r.mca": corrupt_region_bytes()})
    )
    with pytest.raises(IntegrityCheckError):
        await harness.storage.commit_snapshot(handle)
    assert await harness.storage.current_generation(community, server) == 1


async def test_commit_with_matching_expected_base_publishes(
    harness: StorageHarness,
) -> None:
    # Issue #899 commit-time stale guard: when the store has NOT advanced during the
    # upload window (current still equals the base the pre-stream guard validated),
    # the commit publishes normally and bumps the generation.
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"v1"})
    base = await harness.storage.current_generation(community, server)

    handle = await harness.storage.begin_snapshot(community, server)
    await harness.storage.write_snapshot(handle, tar_stream({"f": b"v2"}))
    generation = await harness.storage.commit_snapshot(handle, expected_base=base)
    assert generation == base + 1
    blob = await drain(harness.storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"v2"}


async def test_commit_refused_when_edit_advanced_during_upload(
    harness: StorageHarness,
) -> None:
    # Issue #899: an at-rest edit lands AFTER the pre-stream guard passed (here,
    # after begin/write_snapshot but before commit — the upload window). The commit's
    # expected-base re-check sees current advanced past the base and refuses with
    # StaleGenerationError; the staging is discarded (the handle is consumed, no
    # leak), the generation does NOT bump again, and the just-edited current survives.
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"v1"})
    base = await harness.storage.current_generation(community, server)

    # The worker's upload is staged at the base the guard validated...
    handle = await harness.storage.begin_snapshot(community, server)
    await harness.storage.write_snapshot(handle, tar_stream({"f": b"worker"}))
    # ...then an at-rest edit advances the store during the upload window.
    await harness.storage.write_file(community, server, RelPath("f"), b"edited-at-rest")
    advanced = await harness.storage.current_generation(community, server)
    assert advanced == base + 1

    with pytest.raises(StaleGenerationError) as exc:
        await harness.storage.commit_snapshot(handle, expected_base=base)
    assert exc.value.expected_base == base
    assert exc.value.current == advanced

    # No further bump, and the edit's content — not the stale worker upload — is live.
    assert await harness.storage.current_generation(community, server) == advanced
    assert (
        await harness.storage.read_file(community, server, RelPath("f"))
        == b"edited-at-rest"
    )

    # The consumed handle cannot be reused, and a fresh transfer publishes cleanly,
    # proving the refusal left no leaked staging that blocks future commits.
    with pytest.raises(SnapshotHandleError):
        await harness.storage.commit_snapshot(handle, expected_base=advanced)
    handle = await harness.storage.begin_snapshot(community, server)
    await harness.storage.write_snapshot(handle, tar_stream({"f": b"rebased"}))
    assert (
        await harness.storage.commit_snapshot(handle, expected_base=advanced)
        == advanced + 1
    )


async def test_commit_without_expected_base_skips_recheck(
    harness: StorageHarness,
) -> None:
    # Issue #899: a publish that declares no base (older Worker / never hydrated)
    # passes expected_base=None, which skips the commit-time re-check entirely —
    # backward-compatible with the pre-stream guard's permissive posture. Even if the
    # store advanced, the commit publishes (last flip wins, the prior behaviour).
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"v1"})

    handle = await harness.storage.begin_snapshot(community, server)
    await harness.storage.write_snapshot(handle, tar_stream({"f": b"worker"}))
    await harness.storage.write_file(community, server, RelPath("f"), b"edit")

    generation = await harness.storage.commit_snapshot(handle)
    blob = await drain(harness.storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"worker"}
    assert await harness.storage.current_generation(community, server) == generation


async def test_edit_racing_a_commit_through_the_lock_lands_on_the_postflip_world(
    harness: StorageHarness,
) -> None:
    # Issue #920 bug 1: an in-place edit racing a snapshot commit must not capture its
    # read-set (the live pointer / resolved snapshot dir) BEFORE the per-server lock
    # and then write that stale read back inside it. This drives a GENUINE two-task
    # race: a commit is paused INSIDE its critical section (lock held, pointer flipped
    # but not yet returned to the publish path), an edit is dispatched while it is
    # paused, then the commit is released. On the buggy code the edit had already read
    # the pre-flip world and, after the commit flips + GCs the old prefix/tree, wrote
    # its stale read back -- total world loss on the object adapter, silent edit loss
    # on fs. With the read moved under the lock, the edit re-reads the POST-flip world
    # and both files survive. This test fails on the PR's pre-fix head.
    community, server = new_scope()
    await harness.publish(community, server, {"a": b"A", "b": b"B"})

    entered = asyncio.Event()
    release = asyncio.Event()
    storage = harness.storage

    # Pause the commit INSIDE the per-server lock but BEFORE the pointer flip, so a
    # racing edit that captured its read-set outside the lock observes the PRE-flip
    # world (the bug-1 trigger). The pause point is the flip itself: ``_flip_pointer``
    # on the fixed object code, else ``_publish`` (the pre-fix object path and the fs
    # path both flip inside ``_publish``). Both run under the lock with the flip not
    # yet done.
    if isinstance(storage, ObjectStorage):
        flip_name = "_flip_pointer" if hasattr(storage, "_flip_pointer") else "_publish"
        real_flip = getattr(storage, flip_name)

        async def paused_flip(*args: object, **kwargs: object) -> object:
            entered.set()
            await release.wait()
            return await real_flip(*args, **kwargs)

        setattr(storage, flip_name, paused_flip)
    else:
        assert isinstance(storage, FsStorage)
        loop = asyncio.get_running_loop()
        real_publish = storage._publish
        # ``_publish`` runs on a worker thread holding the threading.Lock and flips the
        # symlink. Block that thread on a plain Event BEFORE the flip while signalling
        # the event loop that the critical section is entered, so the edit thread (also
        # lock-gated, having resolved ``current`` outside the lock on the buggy code)
        # can race it.
        thread_release = threading.Event()

        def paused_publish(
            community_id: CommunityId, server_id: ServerId, staging: Path
        ) -> Path | None:
            loop.call_soon_threadsafe(entered.set)
            thread_release.wait()
            return real_publish(community_id, server_id, staging)

        storage._publish = paused_publish  # type: ignore[method-assign]

        async def _open_release_gate() -> None:
            await release.wait()
            thread_release.set()

        asyncio.ensure_future(_open_release_gate())

    # The worker commit publishes a fresh world; it pauses inside its critical section.
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream({"a": b"A2", "b": b"B2"}))
    commit_task = asyncio.ensure_future(storage.commit_snapshot(handle))
    await entered.wait()

    # Dispatch the racing edit and give it several event-loop ticks to reach as far as
    # it can (its lock acquire on the fixed code; its pre-lock read on the buggy code).
    edit_task = asyncio.ensure_future(
        storage.write_file(community, server, RelPath("a"), b"EDIT")
    )
    for _ in range(20):
        await asyncio.sleep(0)

    release.set()
    await asyncio.gather(commit_task, edit_task)

    # The hydrated world must be COMPLETE: both members present, the edit applied to
    # the committed (post-flip) world rather than clobbering it.
    blob = await drain(harness.storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"a": b"EDIT", "b": b"B2"}
    # Generation bumps are sequential: publish(1) -> commit(2) -> edit(3).
    assert await harness.storage.current_generation(community, server) == 3


async def test_hydrate_streams_incrementally_not_buffered(
    harness: StorageHarness,
) -> None:
    """A multi-MiB working set surfaces as multiple bounded yields, never one
    whole-archive buffer (the bounded-memory contract, Sections 3.1/7.3)."""

    community, server = new_scope()
    payload = b"x" * (3 * 1024 * 1024 + 17)  # larger than the fs/object egress chunk
    # A non-region name: the streaming behaviour under test is file-type-agnostic,
    # and a ``.mca`` here would trip the fs publish integrity gate (#739) on these
    # garbage bytes.
    big = {"world/region.dat": payload}
    # Stage with coarse chunks so the multi-MiB spool write does not dominate.
    handle = await harness.storage.begin_snapshot(community, server)
    await harness.storage.write_snapshot(handle, tar_stream(big, chunk=1024 * 1024))
    await harness.storage.commit_snapshot(handle)

    stream = harness.storage.open_hydrate_source(community, server)
    chunks = [chunk async for chunk in stream]
    assert len(chunks) > 1  # incremental, not one buffered blob
    assert read_tar(b"".join(chunks)) == big


async def test_abort_discards_staging_and_leaves_current_untouched(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"live"})

    handle = await harness.storage.begin_snapshot(community, server)
    await harness.storage.write_snapshot(handle, tar_stream({"f": b"discard-me"}))
    await harness.storage.abort_snapshot(handle)

    blob = await drain(harness.storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"live"}


async def test_abort_is_idempotent(harness: StorageHarness) -> None:
    community, server = new_scope()
    handle = await harness.storage.begin_snapshot(community, server)
    await harness.storage.abort_snapshot(handle)
    await harness.storage.abort_snapshot(handle)  # no raise


async def test_commit_empty_transfer_is_refused(harness: StorageHarness) -> None:
    """An empty staging area is not a publishable transfer (STORAGE.md Section 4.1).

    A ``begin -> commit`` with no staged bytes must be refused by both backends:
    a worker packing an empty working set is a bug signal, never a valid snapshot.
    """

    community, server = new_scope()
    handle = await harness.storage.begin_snapshot(community, server)
    with pytest.raises(IncompleteTransferError):
        await harness.storage.commit_snapshot(handle)


async def test_commit_after_commit_rejects_reused_handle(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    handle = await harness.storage.begin_snapshot(community, server)
    await harness.storage.write_snapshot(handle, tar_stream({"f": b"v1"}))
    await harness.storage.commit_snapshot(handle)
    with pytest.raises((SnapshotHandleError, IncompleteTransferError)):
        await harness.storage.commit_snapshot(handle)


async def test_write_snapshot_sandboxes_malicious_members(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    handle = await harness.storage.begin_snapshot(community, server)

    async def _stream() -> AsyncIterator[bytes]:
        async for chunk in stream_of(malicious_tar_with_escape()):
            yield chunk

    with pytest.raises(Exception):
        await harness.storage.write_snapshot(handle, _stream())


# --- content-integrity gate on the authoritative-create paths (#703/#750) ---


async def test_commit_refuses_corrupt_region_and_preserves_prior(
    harness: StorageHarness,
) -> None:
    """A commit whose staged working set carries a structurally corrupt ``.mca``
    is refused on BOTH backends (#750): the gate raises ``IntegrityCheckError`` and
    the prior published snapshot is left untouched (last-known-good, #703)."""

    community, server = new_scope()
    prior = {"world/region/r.0.0.mca": healthy_region_bytes()}
    await harness.publish(community, server, prior)

    handle = await harness.storage.begin_snapshot(community, server)
    await harness.storage.write_snapshot(
        handle, tar_stream({"world/region/r.0.0.mca": corrupt_region_bytes()})
    )
    with pytest.raises(IntegrityCheckError):
        await harness.storage.commit_snapshot(handle)

    # The corrupt transfer never published: the prior healthy region is still live.
    blob = await drain(harness.storage.open_hydrate_source(community, server))
    assert read_tar(blob) == prior


async def test_commit_publishes_a_structurally_healthy_region(
    harness: StorageHarness,
) -> None:
    """A structurally sound ``.mca`` passes the gate and publishes unchanged."""

    community, server = new_scope()
    files = {"world/region/r.0.0.mca": healthy_region_bytes()}
    await harness.publish(community, server, files)

    blob = await drain(harness.storage.open_hydrate_source(community, server))
    assert read_tar(blob) == files


async def test_running_source_publish_then_backup_create_succeeds(
    harness: StorageHarness,
) -> None:
    """After a commit lands an unpadded set in the store, the at-rest backup-create
    gate must NOT refuse it (issue #923/#927).

    A 26.x server's working set legitimately carries an unpadded tail. Its content was
    gated at publish time, so creating a backup of it must succeed — pre-fix the strict
    create gate raised ``IntegrityCheckError(not_4096_aligned)``, making backups of
    running servers fail deterministically.
    """

    community, server = new_scope()
    await harness.publish(
        community,
        server,
        {"world/region/r.0.0.mca": unaligned_live_region_bytes()},
    )

    key = await harness.storage.create_backup_from_current(community, server)
    assert key in await harness.storage.list_backups(community, server)


async def test_sweep_does_not_quarantine_a_live_format_snapshot(
    harness: StorageHarness,
) -> None:
    """The integrity-sweep snapshot fsck must report a live-format (unpadded) store
    as healthy, not quarantine it (issue #923/#927).

    Pre-fix ``check_current_health`` applied the strict rule to ``current/``, so a
    perfectly healthy unpadded snapshot produced a false ``SNAPSHOT_QUARANTINE``.
    """

    community, server = new_scope()
    await harness.publish(
        community,
        server,
        {"world/region/r.0.0.mca": unaligned_live_region_bytes()},
    )

    report = await harness.storage.check_current_health(community, server)
    assert report.healthy is True


async def test_restore_of_a_live_format_archive_succeeds(
    harness: StorageHarness,
) -> None:
    """A backup created from a live-format ``current/`` is itself unpadded, so the
    restore-direction gate must tolerate it and republish it (issue #923/#927)."""

    community, server = new_scope()
    original = {"world/region/r.0.0.mca": unaligned_live_region_bytes()}
    await harness.publish(community, server, original)
    key = await harness.storage.create_backup_from_current(community, server)

    # Advance current to a different (aligned) set, then restore the live-format
    # backup over it: the restore gate must accept the unpadded archive.
    await harness.publish(
        community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    await harness.storage.restore_backup(community, server, key)

    blob = await drain(harness.storage.open_hydrate_source(community, server))
    assert read_tar(blob) == original


# --- JAR store / reuse (Section 3.2) ---------------------------------------


async def test_put_jar_returns_sha256_content_key(harness: StorageHarness) -> None:
    data = b"jar-bytes-here"
    key = await harness.storage.put_jar(stream_of(data))
    assert key == JarKey(hashlib.sha256(data).hexdigest())


async def test_put_jar_is_idempotent_and_dedupes(harness: StorageHarness) -> None:
    data = b"the-same-jar"
    k1 = await harness.storage.put_jar(stream_of(data))
    k2 = await harness.storage.put_jar(stream_of(data))
    assert k1 == k2


async def test_jar_round_trip(harness: StorageHarness) -> None:
    data = b"x" * (64 * 1024 + 17)
    key = await harness.storage.put_jar(stream_of(data, chunk=4096))
    assert await harness.storage.has_jar(key) is True
    assert await drain(harness.storage.open_jar(key)) == data


async def test_has_jar_false_when_absent(harness: StorageHarness) -> None:
    assert await harness.storage.has_jar(JarKey("a" * 64)) is False


async def test_open_missing_jar_is_not_found(harness: StorageHarness) -> None:
    with pytest.raises(NotFoundError):
        await drain(harness.storage.open_jar(JarKey("b" * 64)))


async def test_jar_pool_stats_empty(harness: StorageHarness) -> None:
    stats = await harness.storage.jar_pool_stats()
    assert stats.count == 0
    assert stats.total_bytes == 0


async def test_jar_pool_stats_counts_and_sums(harness: StorageHarness) -> None:
    a = b"jar-a"
    b = b"jar-bb"
    await harness.storage.put_jar(stream_of(a))
    await harness.storage.put_jar(stream_of(b))
    # Dedup: storing identical bytes again must not double-count (Section 3.2).
    await harness.storage.put_jar(stream_of(a))

    stats = await harness.storage.jar_pool_stats()
    assert stats.count == 2
    assert stats.total_bytes == len(a) + len(b)


async def test_list_jars_returns_keys_sizes_and_mtime(
    harness: StorageHarness,
) -> None:
    a = b"jar-a"
    b = b"jar-bb"
    ka = await harness.storage.put_jar(stream_of(a))
    kb = await harness.storage.put_jar(stream_of(b))

    entries = await harness.storage.list_jars()
    by_key = {e.key: e for e in entries}
    assert set(by_key) == {ka, kb}
    assert by_key[ka].size_bytes == len(a)
    assert by_key[kb].size_bytes == len(b)
    # Each entry carries a timezone-aware modification time (the GC safety window
    # reads it, #293).
    for entry in entries:
        assert entry.modified_at.tzinfo is not None


async def test_list_jars_empty_pool(harness: StorageHarness) -> None:
    assert await harness.storage.list_jars() == []


async def test_delete_jar_removes_it(harness: StorageHarness) -> None:
    key = await harness.storage.put_jar(stream_of(b"to-be-deleted"))
    assert await harness.storage.has_jar(key) is True
    await harness.storage.delete_jar(key)
    assert await harness.storage.has_jar(key) is False


async def test_delete_jar_is_idempotent(harness: StorageHarness) -> None:
    # Deleting an absent jar is a no-op (mirrors delete_backup, #293).
    await harness.storage.delete_jar(JarKey("c" * 64))


# --- backup archive create / list / restore / delete (Section 3.3) ---------


async def test_backup_create_list_restore_round_trip(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    original = {"server.properties": b"k=v", "world/level.dat": b"world"}
    await harness.publish(community, server, original)

    key = await harness.storage.create_backup_from_current(community, server)
    assert key in await harness.storage.list_backups(community, server)

    await harness.publish(community, server, {"server.properties": b"changed"})
    await harness.storage.restore_backup(community, server, key)

    blob = await drain(harness.storage.open_hydrate_source(community, server))
    assert read_tar(blob) == original


async def test_restore_bumps_generation_and_records_sentinel_publisher(
    harness: StorageHarness,
) -> None:
    # A restore is an authoritative publish that replaces ``current/``, so it MUST
    # advance the working-set generation (issue #873) just like a snapshot commit —
    # otherwise a same-worker scratch with held == store would skip the post-restore
    # hydrate (#767) and boot the PRE-restore world. The publisher is stamped with the
    # RESTORE_PUBLISHER sentinel so the publish-time guard refuses an in-flight stale
    # snapshot from a real Worker (different publisher), closing the clobber window.
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"v1"})  # generation 1
    key = await harness.storage.create_backup_from_current(community, server)

    await harness.publish(community, server, {"f": b"v2"})  # generation 2
    assert await harness.storage.current_generation(community, server) == 2

    await harness.storage.restore_backup(community, server, key)
    assert await harness.storage.current_generation(community, server) == 3
    assert (
        await harness.storage.current_publisher(community, server) == RESTORE_PUBLISHER
    )


async def _edit_write_file(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> int:
    await harness.storage.write_file(community, server, RelPath("a.txt"), b"edited")
    return 1


async def _edit_delete_file(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> int:
    await harness.storage.delete_file(community, server, RelPath("a.txt"))
    return 1


async def _edit_delete_dir(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> int:
    await harness.storage.delete_dir(community, server, RelPath("d"))
    return 1


async def _edit_rename_file(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> int:
    await harness.storage.rename_file(
        community, server, RelPath("a.txt"), RelPath("a_renamed.txt")
    )
    return 1


async def _edit_rename_dir(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> int:
    await harness.storage.rename_dir(
        community, server, RelPath("d"), RelPath("d_renamed")
    )
    return 1


async def _edit_make_dir(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> int:
    await harness.storage.make_dir(community, server, RelPath("plugins"))
    return 1


async def _edit_rollback_file(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> int:
    # An edit then a roll back to the captured version: BOTH are authoritative writes
    # (rollback writes the old bytes), so the generation advances twice.
    await harness.storage.write_file(community, server, RelPath("a.txt"), b"v2")
    versions = await harness.storage.list_file_versions(
        community, server, RelPath("a.txt")
    )
    await harness.storage.rollback_file(
        community, server, RelPath("a.txt"), versions[0]
    )
    return 2


@pytest.mark.parametrize(
    "edit",
    [
        _edit_write_file,
        _edit_delete_file,
        _edit_delete_dir,
        _edit_rename_file,
        _edit_rename_dir,
        _edit_make_dir,
        _edit_rollback_file,
    ],
    ids=[
        "write_file",
        "delete_file",
        "delete_dir",
        "rename_file",
        "rename_dir",
        "make_dir",
        "rollback_file",
    ],
)
async def test_authoritative_edit_bumps_generation_with_api_sentinel(
    harness: StorageHarness,
    edit: Callable[[StorageHarness, CommunityId, ServerId], Awaitable[int]],
) -> None:
    # Every authoritative ``current/`` mutation replaces the published world, so it
    # MUST advance the generation and stamp the API_EDIT_PUBLISHER sentinel (issue
    # #889) — otherwise a same-worker scratch with held == store skips the post-edit
    # hydrate (#767) and boots the PRE-edit world, and that scratch's in-flight stale
    # snapshot (same publisher, base == current) clobbers the edit.
    community, server = new_scope()
    await harness.publish(
        community, server, {"a.txt": b"v1", "d/inner": b"x"}
    )  # generation 1
    assert await harness.storage.current_generation(community, server) == 1

    bumps = await edit(harness, community, server)

    assert await harness.storage.current_generation(community, server) == 1 + bumps
    assert (
        await harness.storage.current_publisher(community, server) == API_EDIT_PUBLISHER
    )


async def test_write_file_on_never_published_server_bumps_past_zero(
    harness: StorageHarness,
) -> None:
    # The first write to a never-snapshotted server publishes an initial working set
    # (issue #205). That is an authoritative edit too, so it bumps past generation 0
    # and stamps the API_EDIT_PUBLISHER sentinel (issue #889).
    community, server = new_scope()
    assert await harness.storage.current_generation(community, server) == 0

    await harness.storage.write_file(community, server, RelPath("a.txt"), b"first")

    assert await harness.storage.current_generation(community, server) == 1
    assert (
        await harness.storage.current_publisher(community, server) == API_EDIT_PUBLISHER
    )


async def test_backup_from_current_without_publish_is_not_found(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    with pytest.raises(NotFoundError):
        await harness.storage.create_backup_from_current(community, server)


async def test_restore_unknown_backup_is_not_found(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    with pytest.raises(NotFoundError):
        await harness.storage.restore_backup(community, server, BackupKey("nope"))


async def test_restore_rejects_decompression_bomb(backend: str, tmp_path: Path) -> None:
    """A backup whose members inflate past the restore cap is refused, not extracted.

    The compressed archive is bounded on the way in, but a gzip member can expand
    ~1000x; the restore extraction bounds the cumulative DECOMPRESSED bytes so a
    bomb cannot fill the disk (#287). Built with a tiny cap so the fixture stays
    small.
    """

    harness = build_harness(backend, tmp_path, max_restore_bytes=1024)
    community, server = new_scope()
    bomb = bomb_targz()
    key = await harness.storage.put_backup(community, server, stream_of(bomb))
    with pytest.raises(ArchiveTooLargeError):
        await harness.storage.restore_backup(community, server, key)


async def test_restore_corrupt_backup_without_force_is_refused(
    harness: StorageHarness,
) -> None:
    """Restoring a backup carrying a corrupt ``.mca`` without ``force`` is refused on
    BOTH backends (#750/#743): the gate raises ``IntegrityCheckError`` and the live
    snapshot is left untouched, so a known-corrupt backup never re-poisons it. The
    restore gate runs in live mode (issue #923), so the fixture is a tear the live
    rule still catches (a location entry past EOF), not a mere unaligned size."""

    community, server = new_scope()
    await harness.publish(
        community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    corrupt = region_targz(
        {"world/region/r.0.0.mca": mode_invariant_corrupt_region_bytes()}
    )
    key = await harness.storage.put_backup(community, server, stream_of(corrupt))

    with pytest.raises(IntegrityCheckError):
        await harness.storage.restore_backup(community, server, key)

    # The refused restore never published: the prior healthy region is still live.
    blob = await drain(harness.storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"world/region/r.0.0.mca": healthy_region_bytes()}


async def test_restore_corrupt_backup_with_force_publishes_and_reports(
    harness: StorageHarness,
) -> None:
    """``force=True`` is the operator override: a corrupt backup IS published and the
    returned report flags the corruption so the caller can quarantine + audit it
    (#703 — a deliberate corrupt restore beats no restore)."""

    community, server = new_scope()
    await harness.publish(
        community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    # The restore gate runs in live mode (issue #923), so use a tear the live rule
    # still catches (a location entry past EOF), not a mere unaligned size.
    corrupt_region = mode_invariant_corrupt_region_bytes()
    corrupt = region_targz({"world/region/r.0.0.mca": corrupt_region})
    key = await harness.storage.put_backup(community, server, stream_of(corrupt))

    report = await harness.storage.restore_backup(community, server, key, force=True)
    assert not report.healthy
    assert len(report.corrupt) == 1

    # The forced restore published the (corrupt) backup over the live snapshot.
    blob = await drain(harness.storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"world/region/r.0.0.mca": corrupt_region}


async def test_restore_healthy_backup_reports_healthy(
    harness: StorageHarness,
) -> None:
    """A restore of a structurally sound backup reports healthy on both backends."""

    community, server = new_scope()
    original = {"world/region/r.0.0.mca": healthy_region_bytes()}
    await harness.publish(community, server, original)
    key = await harness.storage.create_backup_from_current(community, server)

    await harness.publish(
        community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    report = await harness.storage.restore_backup(community, server, key)
    assert report.healthy

    blob = await drain(harness.storage.open_hydrate_source(community, server))
    assert read_tar(blob) == original


async def test_delete_backup_is_idempotent(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    key = await harness.storage.create_backup_from_current(community, server)

    await harness.storage.delete_backup(community, server, key)
    assert key not in await harness.storage.list_backups(community, server)
    await harness.storage.delete_backup(community, server, key)  # no raise


# --- prune to final snapshot (DeleteServer reclaim, issue #777) -------------


async def test_prune_drops_working_set_but_keeps_backups(
    harness: StorageHarness,
) -> None:
    # After the prune the working set is gone (hydrate / create now 404), but the
    # existing backup archives are left untouched for the caller to prune.
    community, server = new_scope()
    await harness.publish(community, server, {"world/level.dat": b"w"})
    key = await harness.storage.create_backup_from_current(community, server)

    await harness.storage.prune_to_final_snapshot(community, server)

    with pytest.raises(NotFoundError):
        await drain(harness.storage.open_hydrate_source(community, server))
    with pytest.raises(NotFoundError):
        await harness.storage.create_backup_from_current(community, server)
    assert await harness.storage.list_backups(community, server) == [key]


async def test_prune_without_published_snapshot_is_a_noop(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    await harness.storage.prune_to_final_snapshot(community, server)  # no raise
    with pytest.raises(NotFoundError):
        await drain(harness.storage.open_hydrate_source(community, server))


async def test_prune_after_restore_still_drops_working_set(
    harness: StorageHarness,
) -> None:
    # A restore now bumps the generation (issue #873). Prune keys off the ``current``
    # pointer (not the generation value) and unconditionally drops the marker, so a
    # restore-bumped generation must not break the prune-to-final reclaim (#825/#777):
    # the working set is still collapsed and the backup is retained.
    community, server = new_scope()
    await harness.publish(community, server, {"world/level.dat": b"w"})
    key = await harness.storage.create_backup_from_current(community, server)
    await harness.storage.restore_backup(community, server, key)

    await harness.storage.prune_to_final_snapshot(community, server)

    with pytest.raises(NotFoundError):
        await drain(harness.storage.open_hydrate_source(community, server))
    assert await harness.storage.list_backups(community, server) == [key]


# --- backup transfer: open / put / size (Section 3.3, issue #281) -----------


async def test_open_backup_streams_native_archive_round_trips_via_put(
    harness: StorageHarness,
) -> None:
    """Download (open) then re-upload (put) yields a restorable backup: the bytes
    stream out and back in verbatim, no recompression."""

    community, server = new_scope()
    original = {"server.properties": b"k=v", "world/level.dat": b"world"}
    await harness.publish(community, server, original)
    key = await harness.storage.create_backup_from_current(community, server)

    archive = await drain(harness.storage.open_backup(community, server, key))

    # Upload the same archive bytes to a DIFFERENT server, then restore it.
    other_community, other_server = new_scope()
    new_key = await harness.storage.put_backup(
        other_community, other_server, stream_of(archive)
    )
    assert new_key in await harness.storage.list_backups(other_community, other_server)
    await harness.storage.restore_backup(other_community, other_server, new_key)

    blob = await drain(
        harness.storage.open_hydrate_source(other_community, other_server)
    )
    assert read_tar(blob) == original


async def test_open_unknown_backup_is_not_found(harness: StorageHarness) -> None:
    community, server = new_scope()
    with pytest.raises(NotFoundError):
        await drain(harness.storage.open_backup(community, server, BackupKey("nope")))


async def test_backup_size_reports_archive_byte_count(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    key = await harness.storage.create_backup_from_current(community, server)

    archive = await drain(harness.storage.open_backup(community, server, key))
    assert await harness.storage.backup_size(community, server, key) == len(archive)


async def test_backup_size_unknown_is_not_found(harness: StorageHarness) -> None:
    community, server = new_scope()
    with pytest.raises(NotFoundError):
        await harness.storage.backup_size(community, server, BackupKey("nope"))


# --- file read / edit + version retention (Sections 3.4, 3.5, 5) -----------


async def test_read_file_returns_published_content(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"server.properties": b"motd=hello"})
    assert (
        await harness.storage.read_file(community, server, RelPath("server.properties"))
        == b"motd=hello"
    )


async def test_read_missing_file_is_not_found(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    with pytest.raises(NotFoundError):
        await harness.storage.read_file(community, server, RelPath("missing.txt"))


async def test_open_file_stream_round_trips_content(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"server.properties": b"motd=hello"})
    blob = await drain(
        harness.storage.open_file_stream(
            community, server, RelPath("server.properties")
        )
    )
    assert blob == b"motd=hello"


async def test_open_file_stream_missing_is_not_found(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    with pytest.raises(NotFoundError):
        await drain(
            harness.storage.open_file_stream(community, server, RelPath("missing.txt"))
        )


async def test_open_file_stream_before_any_publish_is_not_found(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    with pytest.raises(NotFoundError):
        await drain(
            harness.storage.open_file_stream(community, server, RelPath("eula.txt"))
        )


async def test_open_file_stream_is_chunked_for_a_multi_chunk_file(
    harness: StorageHarness,
) -> None:
    """A file larger than the egress chunk surfaces as multiple bounded yields,
    never one whole-file buffer (the bounded-memory contract, issue #265)."""

    community, server = new_scope()
    payload = b"y" * (3 * 1024 * 1024 + 17)  # larger than the fs/object egress chunk
    # A non-region name: the multi-chunk egress under test is file-type-agnostic,
    # and a ``.mca`` here would trip the fs publish integrity gate (#739).
    handle = await harness.storage.begin_snapshot(community, server)
    await harness.storage.write_snapshot(
        handle, tar_stream({"world/region.dat": payload}, chunk=1024 * 1024)
    )
    await harness.storage.commit_snapshot(handle)

    chunks = [
        chunk
        async for chunk in harness.storage.open_file_stream(
            community, server, RelPath("world/region.dat")
        )
    ]
    assert len(chunks) > 1  # incremental, not one buffered blob
    assert b"".join(chunks) == payload


async def test_list_dir_lists_entries(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(
        community, server, {"world/level.dat": b"abc", "server.properties": b"k=v"}
    )
    entries = await harness.storage.list_dir(community, server, RelPath("."))
    names = {(e.name, e.is_dir) for e in entries}
    assert ("world", True) in names
    assert ("server.properties", False) in names
    props = next(e for e in entries if e.name == "server.properties")
    assert props.size == 3


async def test_write_file_overwrites_and_retains_prior_version(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"cfg": b"v1"})

    await harness.storage.write_file(community, server, RelPath("cfg"), b"v2")
    assert await harness.storage.read_file(community, server, RelPath("cfg")) == b"v2"

    versions = await harness.storage.list_file_versions(
        community, server, RelPath("cfg")
    )
    assert len(versions) == 1
    assert (
        await harness.storage.read_file_version(
            community, server, RelPath("cfg"), versions[0]
        )
        == b"v1"
    )


async def test_write_file_before_any_publish_initializes_first_version(
    harness: StorageHarness,
) -> None:
    """An at-rest write on a never-snapshotted server publishes the first version.

    A server that crashed before its first snapshot has no published working set;
    a write must initialize the first published version containing just that file
    (issue #205), not raise. The written file is then readable and hydratable.
    """

    community, server = new_scope()
    await harness.storage.write_file(
        community, server, RelPath("eula.txt"), b"eula=true"
    )

    assert (
        await harness.storage.read_file(community, server, RelPath("eula.txt"))
        == b"eula=true"
    )
    blob = await drain(harness.storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"eula.txt": b"eula=true"}


async def test_write_file_before_any_publish_retains_no_version(
    harness: StorageHarness,
) -> None:
    """The initial write creates a fresh file, so it retains no prior version."""

    community, server = new_scope()
    await harness.storage.write_file(
        community, server, RelPath("eula.txt"), b"eula=true"
    )
    assert (
        await harness.storage.list_file_versions(community, server, RelPath("eula.txt"))
        == []
    )


async def test_list_dir_before_any_publish_is_empty(
    harness: StorageHarness,
) -> None:
    """An at-rest listing on a never-snapshotted server is empty, not an error.

    The unpublished working set is treated as empty (issue #205), mirroring the
    data plane's JAR-only hydrate posture.
    """

    community, server = new_scope()
    assert await harness.storage.list_dir(community, server, RelPath(".")) == []


async def test_read_file_before_any_publish_is_not_found(
    harness: StorageHarness,
) -> None:
    """Reading a file on a never-snapshotted server keeps the 404 mapping (#205)."""

    community, server = new_scope()
    with pytest.raises(NotFoundError):
        await harness.storage.read_file(community, server, RelPath("eula.txt"))


async def test_write_file_creates_new_file_without_version(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"existing": b"x"})

    await harness.storage.write_file(community, server, RelPath("new.txt"), b"fresh")
    assert (
        await harness.storage.read_file(community, server, RelPath("new.txt"))
        == b"fresh"
    )
    assert (
        await harness.storage.list_file_versions(community, server, RelPath("new.txt"))
        == []
    )


async def test_write_file_to_working_set_root_is_refused(
    harness: StorageHarness,
) -> None:
    """A write whose ``rel_path`` names the working-set root is refused (#542).

    ``RelPath(".")`` (the file route's default ``path``) names the working set
    root — a directory, not a file. The atomic rename would target the live
    snapshot directory itself; refuse it as a traversal/invalid path rather than
    raising an unhandled ``IsADirectoryError`` (the at-rest 500, issue #542).
    """

    community, server = new_scope()
    await harness.publish(community, server, {"server.properties": b"a=1"})

    with pytest.raises(PathTraversalError):
        await harness.storage.write_file(community, server, RelPath("."), b"x")
    # The published copy is untouched by the refused write.
    assert (
        await harness.storage.read_file(community, server, RelPath("server.properties"))
        == b"a=1"
    )


async def test_write_file_onto_existing_directory_is_refused(
    harness: StorageHarness,
) -> None:
    """A write whose ``rel_path`` names an existing directory is refused (#542).

    Overwriting a directory with file bytes is never a valid edit; refuse it as a
    traversal/invalid path rather than crashing the atomic rename.
    """

    community, server = new_scope()
    await harness.publish(community, server, {"config/server.properties": b"a=1"})

    with pytest.raises(PathTraversalError):
        await harness.storage.write_file(community, server, RelPath("config"), b"x")


async def test_version_retention_is_count_bounded(backend: str, tmp_path: Path) -> None:
    harness = build_harness(backend, tmp_path, version_retention=3)
    community, server = new_scope()
    await harness.publish(community, server, {"cfg": b"v0"})
    for i in range(1, 8):
        await harness.storage.write_file(
            community, server, RelPath("cfg"), f"v{i}".encode()
        )

    versions = await harness.storage.list_file_versions(
        community, server, RelPath("cfg")
    )
    assert len(versions) == 3  # bounded; oldest pruned (Section 5)
    contents = [
        await harness.storage.read_file_version(community, server, RelPath("cfg"), v)
        for v in versions
    ]
    assert contents[0] == b"v6"  # newest-first
    assert b"v0" not in contents and b"v3" not in contents


async def test_rollback_restores_and_is_reversible(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"cfg": b"first"})
    await harness.storage.write_file(community, server, RelPath("cfg"), b"second")

    versions = await harness.storage.list_file_versions(
        community, server, RelPath("cfg")
    )
    await harness.storage.rollback_file(community, server, RelPath("cfg"), versions[0])
    assert (
        await harness.storage.read_file(community, server, RelPath("cfg")) == b"first"
    )

    versions_after = await harness.storage.list_file_versions(
        community, server, RelPath("cfg")
    )
    latest = await harness.storage.read_file_version(
        community, server, RelPath("cfg"), versions_after[0]
    )
    assert latest == b"second"


async def test_retain_file_version_dedups_repeated_identical_snapshots(
    backend: str, tmp_path: Path
) -> None:
    # The retain-only-if-changed primitive (#351): repeated retains of an UNCHANGED
    # current/ file retain exactly one version, so the bounded ring is not churned
    # with identical copies and distinct at-rest versions are not evicted.
    harness = build_harness(backend, tmp_path, version_retention=3)
    community, server = new_scope()
    await harness.publish(community, server, {"cfg": b"frozen"})

    for _ in range(10):
        await harness.storage.retain_file_version(community, server, RelPath("cfg"))

    versions = await harness.storage.list_file_versions(
        community, server, RelPath("cfg")
    )
    assert len(versions) == 1
    assert (
        await harness.storage.read_file_version(
            community, server, RelPath("cfg"), versions[0]
        )
        == b"frozen"
    )
    # current/ is never mutated by a retain.
    assert (
        await harness.storage.read_file(community, server, RelPath("cfg")) == b"frozen"
    )


async def test_retain_file_version_retains_again_when_content_changes(
    harness: StorageHarness,
) -> None:
    # The dedup is only against the NEWEST retained version: a genuinely changed
    # authoritative copy retains a fresh version (#351).
    community, server = new_scope()
    await harness.publish(community, server, {"cfg": b"a"})

    await harness.storage.retain_file_version(community, server, RelPath("cfg"))
    # Change current/ at rest, then retain again.
    await harness.storage.write_file(community, server, RelPath("cfg"), b"bb")
    await harness.storage.retain_file_version(community, server, RelPath("cfg"))

    versions = await harness.storage.list_file_versions(
        community, server, RelPath("cfg")
    )
    # newest-first: the second retain captured the now-current b"bb".
    assert (
        await harness.storage.read_file_version(
            community, server, RelPath("cfg"), versions[0]
        )
        == b"bb"
    )


async def test_retain_file_version_does_not_evict_distinct_at_rest_versions(
    backend: str, tmp_path: Path
) -> None:
    # The core #351 regression: many identical running-edit snapshots of the frozen
    # current/ retain AT MOST ONE version, so they do not churn the bounded ring and
    # evict genuinely distinct at-rest versions. Without the dedup, 20 snapshots
    # would push 20 identical copies and evict every distinct one.
    harness = build_harness(backend, tmp_path, version_retention=3)
    community, server = new_scope()
    # Publish, then snapshot once so the newest retained version equals current/.
    await harness.publish(community, server, {"cfg": b"frozen"})
    await harness.storage.retain_file_version(community, server, RelPath("cfg"))
    # Two distinct at-rest edits add two more versions; the ring now holds three
    # distinct versions (b"frozen", b"v1", b"v2"), and current/ is b"v2".
    await harness.storage.write_file(community, server, RelPath("cfg"), b"v1")
    await harness.storage.write_file(community, server, RelPath("cfg"), b"v2")
    before = await harness.storage.list_file_versions(community, server, RelPath("cfg"))
    assert len(before) == 3
    # The newest retained version (b"v1", retained before the b"v2" overwrite)
    # differs from current/ (b"v2"), so the first running snapshot legitimately
    # retains b"v2" once; arrange the equal case so all 20 snapshots dedup.
    await harness.storage.retain_file_version(community, server, RelPath("cfg"))
    baseline = await harness.storage.list_file_versions(
        community, server, RelPath("cfg")
    )

    # Twenty more identical running-edit snapshots of the still-frozen current/.
    for _ in range(20):
        await harness.storage.retain_file_version(community, server, RelPath("cfg"))

    after = await harness.storage.list_file_versions(community, server, RelPath("cfg"))
    # The ring is unchanged across the 20 repeats: every identical snapshot deduped.
    assert after == baseline
    contents = [
        await harness.storage.read_file_version(community, server, RelPath("cfg"), v)
        for v in after
    ]
    # current/ (b"v2") was retained once, leaving two distinct earlier versions; the
    # 20 repeats added nothing.
    assert contents == [b"v2", b"v1", b"frozen"]


async def test_retain_file_version_missing_file_is_noop(
    harness: StorageHarness,
) -> None:
    # A file with no authoritative copy yet (created while running) is a no-op: no
    # error, no version (#351).
    community, server = new_scope()
    await harness.publish(community, server, {"cfg": b"x"})

    await harness.storage.retain_file_version(community, server, RelPath("absent.txt"))
    assert (
        await harness.storage.list_file_versions(
            community, server, RelPath("absent.txt")
        )
        == []
    )


async def test_retain_file_version_before_any_publish_is_noop(
    harness: StorageHarness,
) -> None:
    # A never-snapshotted server has no authoritative copy at all: retaining is a
    # no-op rather than raising (#351).
    community, server = new_scope()

    await harness.storage.retain_file_version(community, server, RelPath("cfg"))
    assert (
        await harness.storage.list_file_versions(community, server, RelPath("cfg"))
        == []
    )


# --- delete / mkdir (Section 3.4, issue #259) ------------------------------


async def test_delete_file_removes_and_retains_prior_content(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"cfg": b"keep-me", "other": b"x"})

    await harness.storage.delete_file(community, server, RelPath("cfg"))

    with pytest.raises(NotFoundError):
        await harness.storage.read_file(community, server, RelPath("cfg"))
    # The sibling is untouched.
    assert await harness.storage.read_file(community, server, RelPath("other")) == b"x"
    # The deleted content is retained, so a rollback can resurrect it.
    versions = await harness.storage.list_file_versions(
        community, server, RelPath("cfg")
    )
    assert len(versions) == 1
    assert (
        await harness.storage.read_file_version(
            community, server, RelPath("cfg"), versions[0]
        )
        == b"keep-me"
    )


async def test_delete_missing_file_is_not_found(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    with pytest.raises(NotFoundError):
        await harness.storage.delete_file(community, server, RelPath("missing"))


async def test_delete_dir_removes_subtree(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(
        community,
        server,
        {
            "world/level.dat": b"a",
            "world/region/r.dat": b"b",
            "server.properties": b"keep",
        },
    )

    await harness.storage.delete_dir(community, server, RelPath("world"))

    with pytest.raises(NotFoundError):
        await harness.storage.list_dir(community, server, RelPath("world"))
    with pytest.raises(NotFoundError):
        await harness.storage.read_file(
            community, server, RelPath("world/region/r.dat")
        )
    # A sibling outside the deleted subtree survives.
    assert (
        await harness.storage.read_file(community, server, RelPath("server.properties"))
        == b"keep"
    )


async def test_delete_missing_dir_is_not_found(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    with pytest.raises(NotFoundError):
        await harness.storage.delete_dir(community, server, RelPath("nope"))


async def test_make_dir_then_write_file_under_it(harness: StorageHarness) -> None:
    """make_dir followed by a write under it makes the directory observable.

    The empty-directory itself is backend-dependent (fs materializes it; object
    storage cannot represent an empty dir), so the portable contract is: after a
    file is written under the new directory, the directory lists that file.
    """

    community, server = new_scope()
    await harness.publish(community, server, {"server.properties": b"x"})

    await harness.storage.make_dir(community, server, RelPath("plugins"))
    await harness.storage.write_file(
        community, server, RelPath("plugins/p.jar"), b"jar"
    )

    entries = await harness.storage.list_dir(community, server, RelPath("plugins"))
    assert {e.name for e in entries} == {"p.jar"}


async def test_rename_file_moves_without_version_capture(
    harness: StorageHarness,
) -> None:
    """rename_file moves a file atomically, skipping version retention (#1164)."""
    community, server = new_scope()
    await harness.publish(
        community,
        server,
        {"mods/test.jar": b"jar-bytes", "server.properties": b"keep"},
    )

    await harness.storage.rename_file(
        community, server, RelPath("mods/test.jar"), RelPath("mods/test.jar.disabled")
    )

    # The file is at the new path.
    assert (
        await harness.storage.read_file(
            community, server, RelPath("mods/test.jar.disabled")
        )
        == b"jar-bytes"
    )
    # The old path is gone.
    with pytest.raises(NotFoundError):
        await harness.storage.read_file(community, server, RelPath("mods/test.jar"))
    # No version was captured (no retention bloat).
    versions = await harness.storage.list_file_versions(
        community, server, RelPath("mods/test.jar")
    )
    assert versions == []
    versions_new = await harness.storage.list_file_versions(
        community, server, RelPath("mods/test.jar.disabled")
    )
    assert versions_new == []
    # A sibling outside the rename survives.
    assert (
        await harness.storage.read_file(community, server, RelPath("server.properties"))
        == b"keep"
    )


async def test_rename_missing_file_is_not_found(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    with pytest.raises(NotFoundError):
        await harness.storage.rename_file(
            community, server, RelPath("nope.txt"), RelPath("dest.txt")
        )


async def test_rename_dir_moves_subtree(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(
        community,
        server,
        {
            "world/level.dat": b"a",
            "world/region/r.dat": b"b",
            "server.properties": b"keep",
        },
    )

    await harness.storage.rename_dir(
        community, server, RelPath("world"), RelPath("new_world")
    )

    # The new directory has the files.
    entries = await harness.storage.list_dir(community, server, RelPath("new_world"))
    names = {e.name for e in entries}
    assert "level.dat" in names
    assert (
        await harness.storage.read_file(
            community, server, RelPath("new_world/level.dat")
        )
        == b"a"
    )
    assert (
        await harness.storage.read_file(
            community, server, RelPath("new_world/region/r.dat")
        )
        == b"b"
    )
    # The old directory is gone.
    with pytest.raises(NotFoundError):
        await harness.storage.list_dir(community, server, RelPath("world"))
    # A sibling outside the renamed subtree survives.
    assert (
        await harness.storage.read_file(community, server, RelPath("server.properties"))
        == b"keep"
    )


async def test_rename_missing_dir_is_not_found(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    with pytest.raises(NotFoundError):
        await harness.storage.rename_dir(
            community, server, RelPath("nope"), RelPath("dest")
        )


async def test_sweep_never_reclaims_live_snapshot(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"LIVE"})

    await harness.sweep()

    blob = await drain(harness.storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"LIVE"}


def test_tar_bytes_helper_is_stable() -> None:
    # Guards the helper used across both adapters' arrange steps.
    assert read_tar(tar_bytes({"a": b"1"})) == {"a": b"1"}
