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


async def test_active_staging_survives_concurrent_sweep(tmp_path: Path) -> None:
    """An in-flight transfer's staging dir must survive a concurrent sweep.

    The fs adapter pins the staging dir with an in-process active-staging lease for
    the life of the handle (begin -> commit/abort), so a sweep scheduled while the
    transfer is mid-flight skips its incoming/ staging dir (issue #183).
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"LIVE"})

    # Begin + stage an in-flight transfer, but do NOT commit/abort yet.
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream({"f": b"INFLIGHT"}))
    server_root = snapshot_dir(tmp_path, community, server).parent.parent
    incoming = server_root / "incoming"
    assert any(incoming.iterdir())

    # A concurrent sweep must NOT delete the active staging dir.
    storage.sweep()
    assert any(incoming.iterdir()), "active staging must survive a concurrent sweep"

    # The transfer still commits and publishes its staged bytes.
    await storage.commit_snapshot(handle)
    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"INFLIGHT"}


async def test_sweep_reclaims_released_staging_after_abort(tmp_path: Path) -> None:
    """Once a transfer is aborted the staging lease is released; a sweep that finds
    any residual incoming/ dir (here re-seeded) reclaims it — the lease only protects
    in-flight, not released, staging (issue #183)."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"LIVE"})

    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream({"f": b"INFLIGHT"}))
    server_root = snapshot_dir(tmp_path, community, server).parent.parent
    incoming = server_root / "incoming"

    await storage.abort_snapshot(handle)
    # Re-seed a leftover under the (now released) incoming dir to prove the sweep
    # reclaims it now that the lease is gone.
    leftover = incoming / "leftover"
    leftover.mkdir(parents=True, exist_ok=True)
    (leftover / "f").write_bytes(b"x")

    storage.sweep()
    assert not incoming.exists() or not any(incoming.iterdir())


async def test_sweep_reclaims_crash_leftover_staging_with_no_handle(
    tmp_path: Path,
) -> None:
    """Crash leftovers have no in-process handle by definition, so a fresh adapter's
    sweep reclaims them — the lease lives only in the process that began the transfer
    (issue #183)."""

    seeded = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(seeded, community, server, {"f": b"LIVE"})

    # Simulate a crash mid-stage: a staging dir with no live handle (a fresh adapter
    # has an empty active-staging set).
    server_root = snapshot_dir(tmp_path, community, server).parent.parent
    incoming = server_root / "incoming"
    orphan = incoming / "orphan-transfer"
    orphan.mkdir(parents=True, exist_ok=True)
    (orphan / "f").write_bytes(b"PARTIAL")

    recovered = FsStorage(tmp_path)
    recovered.sweep()
    assert not incoming.exists() or not any(incoming.iterdir())
    blob = await drain(recovered.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"LIVE"}
