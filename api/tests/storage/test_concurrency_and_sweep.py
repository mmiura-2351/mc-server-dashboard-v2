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

import os
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


async def test_sweep_reread_skips_snapshot_made_live_after_pointer_read(
    tmp_path: Path,
) -> None:
    """A publish whose new snapshot appeared in the iteration but whose pointer
    flip lands after the sweep started iterating must not delete the now-live
    snapshot (issue #1606).

    The sweep iterates ``snapshots/`` and per-candidate re-reads the ``current``
    symlink: if it now names the candidate, the candidate is live and is skipped.
    Mirrors the object adapter's test for issue #113.
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"OLD"})
    old_snapshot = snapshot_dir(tmp_path, community, server)
    server_root = old_snapshot.parent.parent

    # Simulate a concurrent publisher at AFTER_MOVE stage: a fresh snapshot dir
    # exists under snapshots/ but ``current`` still points at the OLD one. The name
    # sorts after the live OLD snapshot so the sweep encounters OLD first.
    new_snap_dir = server_root / "snapshots" / "zzz-concurrent-new"
    new_snap_dir.mkdir(parents=True)
    (new_snap_dir / "f").write_bytes(b"NEW")

    # Subclass that flips the pointer on the FIRST _live_snapshot_name call (models
    # the publish flip landing after the iteration started but before the guard
    # re-reads for the NEW candidate).
    reads = {"n": 0}

    class _FlipOnFirstRead(FsStorage):
        def _live_snapshot_name(self, sr: Path) -> str | None:
            result = super()._live_snapshot_name(sr)
            if sr == server_root:
                reads["n"] += 1
                if reads["n"] == 1:
                    # Perform the atomic flip: current -> zzz-concurrent-new.
                    link = sr / "current"
                    tmp_link = sr / ".current.flip"
                    os.symlink(
                        os.path.join("snapshots", "zzz-concurrent-new"), tmp_link
                    )
                    os.replace(tmp_link, link)
            return result

    flipping = _FlipOnFirstRead(tmp_path)
    flipping.sweep()

    # The guard must have re-read the pointer at least twice (once per candidate).
    assert reads["n"] >= 2, "the guard must re-read the pointer per candidate"
    # The just-made-live snapshot survived the sweep.
    assert new_snap_dir.exists()
    assert (new_snap_dir / "f").read_bytes() == b"NEW"
    # open_hydrate_source reads the NEW content through the flipped pointer.
    blob = await drain(flipping.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"NEW"}

    # The now-superseded OLD snapshot is reclaimed by a follow-up sweep with no
    # concurrent publisher.
    FsStorage(tmp_path).sweep()
    assert not old_snapshot.exists()


async def test_hydrate_reader_rereads_current_when_reclaim_lands_in_lease_gap(
    tmp_path: Path,
) -> None:
    """A concurrent restore flips+reclaims in the window between resolving
    ``current`` and leasing it (issue #1607). The reader must re-verify and
    converge on the NEW snapshot rather than reading from a reclaimed path."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"OLD"})

    old_snapshot = snapshot_dir(tmp_path, community, server)
    server_root = old_snapshot.parent.parent

    # Seed the new snapshot on disk and prepare the flip+reclaim that a
    # concurrent restore would perform.
    new_snap_dir = server_root / "snapshots" / "new-snap"
    new_snap_dir.mkdir(parents=True)
    (new_snap_dir / "f").write_bytes(b"NEW")

    call_count = {"n": 0}
    original_current_dir = FsStorage._current_dir

    def _racing_current_dir(self: FsStorage, cid: object, sid: object) -> Path:
        call_count["n"] += 1
        result = original_current_dir(self, cid, sid)  # type: ignore[arg-type]
        if call_count["n"] == 1:
            # Simulate the concurrent restore: flip the pointer and reclaim old.
            link = server_root / "current"
            tmp_link = server_root / ".current.race"
            os.symlink(os.path.join("snapshots", "new-snap"), tmp_link)
            os.replace(tmp_link, link)
            import shutil

            shutil.rmtree(old_snapshot)
        return result

    storage._current_dir = _racing_current_dir.__get__(storage, FsStorage)  # type: ignore[method-assign]

    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"NEW"}


async def test_file_stream_rereads_current_when_reclaim_lands_in_lease_gap(
    tmp_path: Path,
) -> None:
    """Same as hydrate but for open_file_stream (issue #1607): a concurrent
    restore in the resolve-lease gap must not yield a stale/deleted file."""

    from mc_server_dashboard_api.storage.domain.value_objects import RelPath

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"OLD"})

    old_snapshot = snapshot_dir(tmp_path, community, server)
    server_root = old_snapshot.parent.parent

    new_snap_dir = server_root / "snapshots" / "new-snap"
    new_snap_dir.mkdir(parents=True)
    (new_snap_dir / "f").write_bytes(b"NEW")

    call_count = {"n": 0}
    original_current_dir = FsStorage._current_dir

    def _racing_current_dir(self: FsStorage, cid: object, sid: object) -> Path:
        call_count["n"] += 1
        result = original_current_dir(self, cid, sid)  # type: ignore[arg-type]
        if call_count["n"] == 1:
            link = server_root / "current"
            tmp_link = server_root / ".current.race"
            os.symlink(os.path.join("snapshots", "new-snap"), tmp_link)
            os.replace(tmp_link, link)
            import shutil

            shutil.rmtree(old_snapshot)
        return result

    storage._current_dir = _racing_current_dir.__get__(storage, FsStorage)  # type: ignore[method-assign]

    blob = await drain(storage.open_file_stream(community, server, RelPath("f")))
    assert blob == b"NEW"
