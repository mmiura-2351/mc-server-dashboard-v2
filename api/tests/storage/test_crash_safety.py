"""Crash-safety at every Section 4.3 publish crash point (STORAGE.md Section 4).

For each named publish phase we inject a simulated process kill (the failure
seam) and assert the invariant that anchors the whole table:

    ``current`` always resolves to ONE COMPLETE snapshot — never absent, never
    partial.

Then we run the idempotent startup sweep and assert it reclaims the orphan a
crash left behind without ever touching the live snapshot, and that a second
sweep is a no-op (recovery is idempotent).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mc_server_dashboard_api.storage.adapters.failure_seam import (
    CrashAt,
    InjectedCrash,
    PublishPhase,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import CommunityId, ServerId
from tests.storage.helpers import drain, new_scope, read_tar, snapshot_dir, tar_stream


async def _seed_initial(
    tmp_path: Path, community: CommunityId, server: ServerId
) -> None:
    """Publish a first complete snapshot so a crash has a prior state to fall to."""

    storage = FsStorage(tmp_path)
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream({"f": b"OLD"}))
    await storage.commit_snapshot(handle)


async def _assert_current_resolves_complete(
    tmp_path: Path,
    community: CommunityId,
    server: ServerId,
    expected_one_of: list[dict[str, bytes]],
) -> None:
    """current must resolve to a directory whose content equals one complete state."""

    storage = FsStorage(tmp_path)
    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) in expected_one_of


# The publish-phase crash points from the Section 4.3 fs/remote-fs column. For
# AFTER_STAGE / AFTER_MOVE / (flip not yet done) current must still be OLD; for
# AFTER_FLIP / AFTER_FSYNC current is already NEW (the flip is the atomic point).
_BEFORE_FLIP = [PublishPhase.AFTER_STAGE, PublishPhase.AFTER_MOVE]
_AFTER_FLIP = [PublishPhase.AFTER_FLIP, PublishPhase.AFTER_FSYNC]

OLD = {"f": b"OLD"}
NEW = {"f": b"NEW"}


@pytest.mark.parametrize("phase", _BEFORE_FLIP)
async def test_crash_before_flip_keeps_old_snapshot_live(
    tmp_path: Path, phase: PublishPhase
) -> None:
    community, server = new_scope()
    await _seed_initial(tmp_path, community, server)
    old_live = snapshot_dir(tmp_path, community, server)

    crashed = FsStorage(tmp_path, failure_seam=CrashAt(phase))
    handle = await crashed.begin_snapshot(community, server)
    await crashed.write_snapshot(handle, tar_stream(NEW))
    with pytest.raises(InjectedCrash):
        await crashed.commit_snapshot(handle)

    # Invariant: current still resolves to the OLD complete snapshot.
    assert snapshot_dir(tmp_path, community, server) == old_live
    await _assert_current_resolves_complete(tmp_path, community, server, [OLD])


@pytest.mark.parametrize("phase", _AFTER_FLIP)
async def test_crash_after_flip_keeps_new_snapshot_live(
    tmp_path: Path, phase: PublishPhase
) -> None:
    community, server = new_scope()
    await _seed_initial(tmp_path, community, server)

    crashed = FsStorage(tmp_path, failure_seam=CrashAt(phase))
    handle = await crashed.begin_snapshot(community, server)
    await crashed.write_snapshot(handle, tar_stream(NEW))
    with pytest.raises(InjectedCrash):
        await crashed.commit_snapshot(handle)

    # Invariant: the flip already happened, so current resolves to NEW complete.
    await _assert_current_resolves_complete(tmp_path, community, server, [NEW])


@pytest.mark.parametrize("phase", _BEFORE_FLIP + _AFTER_FLIP)
async def test_sweep_reclaims_orphan_after_crash_idempotently(
    tmp_path: Path, phase: PublishPhase
) -> None:
    community, server = new_scope()
    await _seed_initial(tmp_path, community, server)

    crashed = FsStorage(tmp_path, failure_seam=CrashAt(phase))
    handle = await crashed.begin_snapshot(community, server)
    await crashed.write_snapshot(handle, tar_stream(NEW))
    with pytest.raises(InjectedCrash):
        await crashed.commit_snapshot(handle)

    recovered = FsStorage(tmp_path)
    live_before_sweep = snapshot_dir(tmp_path, community, server)

    recovered.sweep()

    # The live snapshot survives the sweep (never reclaimed).
    assert snapshot_dir(tmp_path, community, server) == live_before_sweep
    server_root = live_before_sweep.parent.parent
    # Exactly one snapshot remains: the live one. Orphans are gone.
    assert [p.name for p in (server_root / "snapshots").iterdir()] == [
        live_before_sweep.name
    ]
    # Staging is gone.
    incoming = server_root / "incoming"
    assert not incoming.exists() or not any(incoming.iterdir())

    # Idempotent: a second sweep changes nothing and the data is still readable.
    recovered.sweep()
    assert snapshot_dir(tmp_path, community, server) == live_before_sweep
    blob = await drain(recovered.open_hydrate_source(community, server))
    assert read_tar(blob) in [OLD, NEW]


async def test_sweep_on_empty_root_is_noop(tmp_path: Path) -> None:
    FsStorage(tmp_path).sweep()  # no communities/ yet; must not raise


async def test_first_publish_crash_leaves_no_current(tmp_path: Path) -> None:
    """A crash during the very first publish (no prior snapshot) leaves current absent.

    The invariant tolerates this: current is absent only when nothing was ever
    published. The sweep then clears the orphan and the next publish succeeds.
    """

    community, server = new_scope()
    crashed = FsStorage(tmp_path, failure_seam=CrashAt(PublishPhase.AFTER_MOVE))
    handle = await crashed.begin_snapshot(community, server)
    await crashed.write_snapshot(handle, tar_stream(NEW))
    with pytest.raises(InjectedCrash):
        await crashed.commit_snapshot(handle)

    recovered = FsStorage(tmp_path)
    recovered.sweep()
    server_root = (
        tmp_path / "communities" / str(community.value) / "servers" / str(server.value)
    )
    assert not (server_root / "current").exists()
    assert list((server_root / "snapshots").iterdir()) == []

    # A fresh publish recovers cleanly.
    handle = await recovered.begin_snapshot(community, server)
    await recovered.write_snapshot(handle, tar_stream(NEW))
    await recovered.commit_snapshot(handle)
    blob = await drain(recovered.open_hydrate_source(community, server))
    assert read_tar(blob) == NEW


def _prune_server_root(
    tmp_path: Path, community: CommunityId, server: ServerId
) -> Path:
    return (
        tmp_path / "communities" / str(community.value) / "servers" / str(server.value)
    )


async def test_prune_retry_after_crash_keeps_final_and_finishes_gc(
    tmp_path: Path,
) -> None:
    # Crash-retry regression (#777): the prune unlinks the ``current`` symlink the
    # instant final.tar.gz is durable, so a crash AFTER the unlink but before the
    # tree GC leaves: final present + no current symlink + a leftover snapshots/
    # tree. The retried DeleteServer must finish the GC WITHOUT re-packing, so it
    # never overwrites the good final.tar.gz with a partial pack from the leftover.
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(NEW))
    await storage.commit_snapshot(handle)

    # First attempt completes the durable part (final written, current unlinked).
    await storage.prune_to_final_snapshot(community, server)
    server_root = _prune_server_root(tmp_path, community, server)
    final = server_root / "final.tar.gz"
    good_bytes = final.read_bytes()
    assert read_tar(good_bytes) == NEW
    assert not (server_root / "current").exists()

    # Simulate a crash that left the tree GC unfinished: a stray snapshots/ tree and
    # a leaked ``.final.*.tmp`` spool survive. The current symlink stays absent (it
    # is unlinked before any GC).
    stale = server_root / "snapshots" / "dead"
    stale.mkdir(parents=True)
    (stale / "level.dat").write_bytes(b"stale")
    leaked_tmp = server_root / ".final.deadbeef.tmp"
    leaked_tmp.write_bytes(b"junk")

    # The retry takes the no-current branch: GC completes, the leaked tmp is swept,
    # final is byte-for-byte untouched (no partial re-pack), nothing republished.
    await storage.prune_to_final_snapshot(community, server)
    assert final.read_bytes() == good_bytes
    assert read_tar(final.read_bytes()) == NEW
    assert not (server_root / "snapshots").exists()
    assert not (server_root / "current").exists()
    assert not leaked_tmp.exists()
    assert not list(server_root.glob(".final.*.tmp"))


async def test_prune_drops_generation_marker(tmp_path: Path) -> None:
    # Parity with the object adapter (#777 review): the fs prune drops the generation
    # marker too, so the post-delete tree holds only backups/ + final.tar.gz.
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(NEW))
    await storage.commit_snapshot(handle)
    server_root = _prune_server_root(tmp_path, community, server)
    assert (server_root / "generation").exists()

    await storage.prune_to_final_snapshot(community, server)

    assert (server_root / "final.tar.gz").exists()
    assert not (server_root / "generation").exists()
    assert not (server_root / "snapshots").exists()
    assert not (server_root / "current").exists()
