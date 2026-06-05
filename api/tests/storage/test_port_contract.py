"""Backend-agnostic Storage Port contract, parametrized over fs + object (#105).

These assertions depend only on the Port surface and its observable guarantees
(STORAGE.md Sections 3, 4) — never on a backend's internal layout — so they run
unchanged against both adapters via the ``harness`` fixture (conftest). Backend
mechanics (fs symlinks / crash phases; object pointer flip / prefix sweep) are
covered in the per-adapter files.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mc_server_dashboard_api.storage.domain.errors import (
    IncompleteTransferError,
    NotFoundError,
    SnapshotHandleError,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    BackupKey,
    JarKey,
    RelPath,
)
from tests.storage.conftest import StorageHarness, build_harness
from tests.storage.helpers import (
    drain,
    malicious_tar_with_escape,
    new_scope,
    read_tar,
    stream_of,
    tar_bytes,
    tar_stream,
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


async def test_hydrate_streams_incrementally_not_buffered(
    harness: StorageHarness,
) -> None:
    """A multi-MiB working set surfaces as multiple bounded yields, never one
    whole-archive buffer (the bounded-memory contract, Sections 3.1/7.3)."""

    community, server = new_scope()
    payload = b"x" * (3 * 1024 * 1024 + 17)  # larger than the fs/object egress chunk
    big = {"world/region.mca": payload}
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


async def test_delete_backup_is_idempotent(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    key = await harness.storage.create_backup_from_current(community, server)

    await harness.storage.delete_backup(community, server, key)
    assert key not in await harness.storage.list_backups(community, server)
    await harness.storage.delete_backup(community, server, key)  # no raise


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
    handle = await harness.storage.begin_snapshot(community, server)
    await harness.storage.write_snapshot(
        handle, tar_stream({"world/region.mca": payload}, chunk=1024 * 1024)
    )
    await harness.storage.commit_snapshot(handle)

    chunks = [
        chunk
        async for chunk in harness.storage.open_file_stream(
            community, server, RelPath("world/region.mca")
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
            "world/region/r.mca": b"b",
            "server.properties": b"keep",
        },
    )

    await harness.storage.delete_dir(community, server, RelPath("world"))

    with pytest.raises(NotFoundError):
        await harness.storage.list_dir(community, server, RelPath("world"))
    with pytest.raises(NotFoundError):
        await harness.storage.read_file(
            community, server, RelPath("world/region/r.mca")
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


async def test_sweep_never_reclaims_live_snapshot(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"LIVE"})

    await harness.sweep()

    blob = await drain(harness.storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"LIVE"}


def test_tar_bytes_helper_is_stable() -> None:
    # Guards the helper used across both adapters' arrange steps.
    assert read_tar(tar_bytes({"a": b"1"})) == {"a": b"1"}
