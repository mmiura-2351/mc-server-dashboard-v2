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


async def test_prune_fsyncs_final_before_unlinking_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Durability ordering (#777 review): final.tar.gz must be made durable BEFORE the
    # ``current`` symlink is unlinked, or a power cut could commit the unlink/GC while
    # the tar's data blocks were never flushed, destroying the only re-packable source
    # and leaving a torn final. A torn-final power-loss is not reproducible in-process,
    # so we instead assert the ordering: both the tmp-tar fsync and the server_root dir
    # fsync are issued before the ``current`` symlink is removed. We classify each
    # fsync by the fd's type (``stat.S_ISDIR`` over ``os.fstat``) so a reorder that
    # moved the *directory* fsync after the unlink is caught — a bare "some fsync
    # precedes the unlink" check would still pass on the tmp-tar fsync alone (#837).
    import os
    import stat as stat_module

    import mc_server_dashboard_api.storage.adapters.fs as fs_module

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(NEW))
    await storage.commit_snapshot(handle)
    server_root = _prune_server_root(tmp_path, community, server)
    current_link = server_root / "current"

    events: list[str] = []
    real_fsync = os.fsync
    real_rmtree = fs_module._rmtree

    def spy_fsync(fd: int) -> None:
        is_dir = stat_module.S_ISDIR(os.fstat(fd).st_mode)
        events.append("fsync-dir" if is_dir else "fsync-file")
        real_fsync(fd)

    def spy_rmtree(path: Path) -> None:
        if path == current_link:
            events.append("unlink-current")
        real_rmtree(path)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(fs_module, "_rmtree", spy_rmtree)

    await storage.prune_to_final_snapshot(community, server)

    assert "unlink-current" in events
    unlink_at = events.index("unlink-current")
    # The tmp-tar (file) fsync makes the archive's data blocks durable, and the
    # server_root (dir) fsync makes the final rename durable — BOTH must precede the
    # ``current`` unlink, or the unlink/GC could commit while either was unflushed.
    assert "fsync-file" in events and events.index("fsync-file") < unlink_at
    assert "fsync-dir" in events and events.index("fsync-dir") < unlink_at


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


async def test_create_backup_fsyncs_archive_before_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Single-file write discipline (#837): create_backup_from_current must pack into a
    # temp sibling, fsync the tar's data blocks, THEN rename it into ``<key>.tar.gz``,
    # then fsync the directory — so a power cut never leaves a torn archive listed as a
    # normal backup. A torn-archive power-loss is not reproducible in-process, so we
    # assert the ordering: the temp-file fsync precedes the rename onto the final
    # ``.tar.gz`` path, and the directory fsync follows it. fds are classified by type
    # (``stat.S_ISDIR``) so the dir fsync can't masquerade as the file fsync.
    import os
    import stat as stat_module

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(NEW))
    await storage.commit_snapshot(handle)

    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def spy_fsync(fd: int) -> None:
        is_dir = stat_module.S_ISDIR(os.fstat(fd).st_mode)
        events.append("fsync-dir" if is_dir else "fsync-file")
        real_fsync(fd)

    def spy_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if str(dst).endswith(".tar.gz"):
            events.append("rename-archive")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)

    key = await storage.create_backup_from_current(community, server)

    server_root = _prune_server_root(tmp_path, community, server)
    archive = server_root / "backups" / f"{key.value}.tar.gz"
    assert archive.is_file()
    assert read_tar(archive.read_bytes()) == NEW
    assert not list((server_root / "backups").glob(".backup.*.tmp"))

    assert "rename-archive" in events
    rename_at = events.index("rename-archive")
    # The tmp-tar (file) fsync makes the archive's data blocks durable BEFORE the
    # rename publishes the listable name; the directory fsync makes that rename durable
    # AFTER it.
    assert "fsync-file" in events and events.index("fsync-file") < rename_at
    assert events.index("fsync-dir") > rename_at
