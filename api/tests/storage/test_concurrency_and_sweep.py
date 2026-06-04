"""Concurrent publish vs. read, and sweep-never-reclaims-live (STORAGE.md Section 4).

A hydrate stream opened over the old snapshot survives a concurrent publish that
flips ``current`` (POSIX semantics reliance, Section 4.2: the superseded snapshot
is reclaimed only after the flip, and a directory held open is not unlinked out
from under a reader). The sweep, keyed off the live ``current`` target, never
reclaims authoritative data even with extra orphan snapshots around.
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


async def test_hydrate_stream_survives_concurrent_publish(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"OLD"})

    # Open the hydrate stream against the OLD snapshot but do NOT drain it yet.
    stream = storage.open_hydrate_source(community, server)
    first_chunk = await stream.__anext__()

    # Now a concurrent publish flips current to NEW and reclaims OLD.
    await publish(storage, community, server, {"f": b"NEW"})

    # The in-flight stream still yields the complete OLD bytes: the tar of the old
    # snapshot was buffered at open time, so the flip+reclaim cannot tear it.
    rest = await drain(stream)
    blob = first_chunk + rest
    assert read_tar(blob) == {"f": b"OLD"}

    # And a fresh hydrate sees NEW.
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
