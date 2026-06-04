"""Concurrent publish vs. read, and sweep-never-reclaims-live (STORAGE.md Section 4).

The hydrate stream is now generated incrementally (true streaming), so buffering
the whole tar at open time no longer protects an in-flight reader from a
concurrent publish. Instead ``open_hydrate_source`` takes an active-reader lease
on the resolved snapshot; the publish reclaim and the sweep skip a leased
snapshot, and it is reclaimed only once the reader releases (Section 4.2 reader
safety). The sweep, keyed off the live ``current`` target, never reclaims
authoritative data even with extra orphan snapshots around.
"""

from __future__ import annotations

from pathlib import Path

from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from tests.storage.helpers import (
    drain,
    new_scope,
    publish,
    read_tar,
    snapshot_dir,
    tar_stream,
)


async def test_hydrate_lease_survives_publish_then_reclaims_on_release(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"OLD"})
    old_snapshot = snapshot_dir(tmp_path, community, server)

    # Open the hydrate stream against the OLD snapshot but do NOT drain it yet:
    # opening it registers the active-reader lease on the OLD snapshot.
    stream = storage.open_hydrate_source(community, server)
    first_chunk = await stream.__anext__()

    # A concurrent publish flips current to NEW and runs reclaim. Because the OLD
    # snapshot is leased, reclaim skips it and the directory is still on disk.
    await publish(storage, community, server, {"f": b"NEW"})
    assert old_snapshot.exists()  # lease deferred the reclaim

    # The in-flight stream still yields the complete OLD bytes, then releases the
    # lease when it finishes.
    rest = await drain(stream)
    assert read_tar(first_chunk + rest) == {"f": b"OLD"}

    # With the lease released, the next sweep reclaims the superseded OLD snapshot;
    # the live NEW snapshot is untouched.
    storage.sweep()
    assert not old_snapshot.exists()
    new_blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(new_blob) == {"f": b"NEW"}


async def test_sweep_never_reclaims_live_snapshot(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"LIVE"})
    live = snapshot_dir(tmp_path, community, server)

    # Drop an unrelated orphan snapshot dir next to the live one.
    orphan = live.parent / "orphan-snapshot"
    orphan.mkdir()
    (orphan / "junk").write_bytes(b"junk")

    storage.sweep()

    assert live.exists()  # live untouched
    assert not orphan.exists()  # orphan reclaimed
    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"LIVE"}


async def test_sweep_clears_leftover_staging(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"LIVE"})

    # Begin a transfer but never commit/abort -> leftover staging (a crash mid-stage).
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream({"f": b"PARTIAL"}))
    server_root = snapshot_dir(tmp_path, community, server).parent.parent
    assert any((server_root / "incoming").iterdir())

    storage.sweep()
    incoming = server_root / "incoming"
    assert not incoming.exists() or not any(incoming.iterdir())
    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"LIVE"}
