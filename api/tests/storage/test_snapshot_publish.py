"""fs-specific snapshot/publish mechanics: symlink flip + Section 2 layout.

The backend-agnostic snapshot/hydrate/abort/commit contract is in
``test_port_contract.py`` (run against both adapters). This file keeps only the
fs realization details — the ``current`` symlink target, the on-disk Section 2
layout, fs reclaim of the superseded snapshot directory, the incremental
pipe-streamed hydrate bounded by the fs ``_CHUNK``, and the fs symlink-escape
member rejection — which reach into the filesystem tree and so cannot be
backend-neutral.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.errors import (
    IntegrityCheckError,
    MissingRegionsError,
    StaleGenerationError,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    BackupKey,
    CommunityId,
    ServerId,
)
from mc_server_dashboard_api.storage.integrity.region import ReasonCode
from tests.storage.helpers import (
    corrupt_region_bytes,
    drain,
    healthy_region_bytes,
    mode_invariant_corrupt_region_bytes,
    new_scope,
    read_tar,
    region_targz,
    snapshot_dir,
    tar_stream,
)


async def _publish(
    storage: FsStorage,
    community: CommunityId,
    server: ServerId,
    files: dict[str, bytes],
    *,
    publisher: str | None = None,
) -> None:
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(files))
    await storage.commit_snapshot(handle, publisher=publisher)


async def test_generation_and_publisher_share_one_atomic_marker(
    tmp_path: Path,
) -> None:
    # Issue #847: the generation and the publishing Worker id are written as ONE
    # atomic marker, not two separate files. A crash between two separate writes could
    # leave the PREVIOUS publisher attributed to the NEW generation, which would invert
    # the publish-time guard. Asserting a single ``generation`` marker (no separate
    # ``publisher`` file) holding BOTH proves the pair can never diverge: the temp +
    # atomic rename makes (generation, publisher) all-or-nothing.
    storage = FsStorage(tmp_path)
    community, server = new_scope()

    await _publish(
        storage, community, server, {"server.properties": b"a=b"}, publisher="worker-a"
    )

    server_root = (
        tmp_path / "communities" / str(community.value) / "servers" / str(server.value)
    )
    # Exactly one marker file, named ``generation`` — no separate ``publisher`` file.
    assert (server_root / "generation").is_file()
    assert not (server_root / "publisher").exists()
    # The single marker holds BOTH the generation (line 1) and the publisher (line 2).
    lines = (server_root / "generation").read_text().splitlines()
    assert lines == ["1", "worker-a"]
    # And the Port reads both back consistently.
    assert await storage.current_generation(community, server) == 1
    assert await storage.current_publisher(community, server) == "worker-a"

    # A publish with no declared id writes the generation alone (no publisher line),
    # so the guard stays permissive — still a single marker, never a stale id.
    await _publish(storage, community, server, {"server.properties": b"c=d"})
    assert (server_root / "generation").read_text().splitlines() == ["2"]
    assert await storage.current_publisher(community, server) is None


async def test_commit_publishes_current_symlink_to_a_snapshot(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()

    await _publish(storage, community, server, {"server.properties": b"motd=hi"})

    server_root = (
        tmp_path / "communities" / str(community.value) / "servers" / str(server.value)
    )
    link = server_root / "current"
    assert link.is_symlink()
    # current -> snapshots/<id>/ (relative target, Section 4.2)
    target = os.readlink(link)
    assert target.startswith("snapshots" + os.sep)
    assert (server_root / target).is_dir()


async def test_layout_conformance_matches_section_2(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await _publish(storage, community, server, {"world/level.dat": b"x"})

    server_root = (
        tmp_path / "communities" / str(community.value) / "servers" / str(server.value)
    )
    assert (server_root / "current").is_symlink()
    assert (server_root / "snapshots").is_dir()
    live = snapshot_dir(tmp_path, community, server)
    assert (live / "world" / "level.dat").read_bytes() == b"x"


async def test_hydrate_streams_incrementally_not_buffered(tmp_path: Path) -> None:
    """A working set larger than one chunk is yielded in multiple bounded chunks.

    Memory-bound evidence specific to the fs adapter: the hydrate tar is generated
    incrementally (pipe + ``tarfile`` stream mode), so a payload several chunks
    long surfaces as several yields rather than one whole-archive buffer; peak
    memory is one pipe buffer plus one ``_CHUNK``.
    """

    from mc_server_dashboard_api.storage.adapters.fs import _CHUNK
    from tests.storage.helpers import stream_of, tar_bytes

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    # A large non-region payload: the multi-chunk streaming behaviour under test is
    # unrelated to file type, and a ``.mca`` name would now trip the publish
    # integrity gate (issue #739) on this garbage-byte fixture.
    big = {"world/region.dat": b"x" * (3 * _CHUNK)}

    async def _coarse() -> AsyncIterator[bytes]:
        async for chunk in stream_of(tar_bytes(big), chunk=_CHUNK):
            yield chunk

    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, _coarse())
    await storage.commit_snapshot(handle)

    stream = storage.open_hydrate_source(community, server)
    chunks = [chunk async for chunk in stream]
    assert len(chunks) > 1  # incremental, not one buffered blob
    assert all(len(c) <= _CHUNK for c in chunks)  # each yield is bounded
    assert read_tar(b"".join(chunks)) == big


async def test_second_publish_supersedes_and_reclaims_old_snapshot(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await _publish(storage, community, server, {"f": b"v1"})
    first_live = snapshot_dir(tmp_path, community, server)

    await _publish(storage, community, server, {"f": b"v2"})
    second_live = snapshot_dir(tmp_path, community, server)

    assert second_live != first_live
    assert not first_live.exists()  # superseded snapshot reclaimed (Section 4.3)
    snapshots = second_live.parent
    assert [p.name for p in snapshots.iterdir()] == [second_live.name]
    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"v2"}


async def test_abort_discards_staging_and_leaves_current_untouched(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await _publish(storage, community, server, {"f": b"live"})
    live_before = snapshot_dir(tmp_path, community, server)

    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream({"f": b"discard-me"}))
    await storage.abort_snapshot(handle)

    server_root = live_before.parent.parent
    incoming = server_root / "incoming"
    assert not incoming.exists() or not any(incoming.iterdir())
    assert snapshot_dir(tmp_path, community, server) == live_before


async def test_commit_refuses_a_corrupt_region_and_keeps_prior_snapshot(
    tmp_path: Path,
) -> None:
    """The integrity gate (issue #739): a corrupt ``.mca`` in staging is not published.

    A working set carrying a structurally corrupt region file must be refused at
    ``commit_snapshot`` with :class:`IntegrityCheckError` carrying the report; the
    prior ``current`` is left resolving to the last good snapshot and the corrupt
    staging area is cleaned (last-known-good retention, #703).
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await _publish(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    good_live = snapshot_dir(tmp_path, community, server)

    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(
        handle, tar_stream({"world/region/r.0.0.mca": corrupt_region_bytes()})
    )
    with pytest.raises(IntegrityCheckError) as excinfo:
        await storage.commit_snapshot(handle)

    # The report names the corrupt file and its reason so a caller can surface why.
    report = excinfo.value.report
    assert len(report.corrupt) == 1
    assert report.corrupt[0].reason is ReasonCode.NOT_4096_ALIGNED

    # current still resolves to the prior good snapshot; staging was cleaned.
    assert snapshot_dir(tmp_path, community, server) == good_live
    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"world/region/r.0.0.mca": healthy_region_bytes()}
    server_root = good_live.parent.parent
    incoming = server_root / "incoming"
    assert not incoming.exists() or not any(incoming.iterdir())


async def test_commit_publishes_a_healthy_region_unchanged(tmp_path: Path) -> None:
    """A healthy working set publishes exactly as before (no gate regression)."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    files = {
        "world/region/r.0.0.mca": healthy_region_bytes(),
        "server.properties": b"x",
    }
    await _publish(storage, community, server, files)

    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == files


async def test_commit_refuses_partial_region_loss_and_keeps_prior(
    tmp_path: Path,
) -> None:
    """The missing-region gate (issue #854): a publish that DROPS some-but-not-all of
    a live dimension's region files is refused.

    Every other gate validates only files that exist, so a vanished region is
    structurally valid absence. A staged set that lost a region a dimension still
    populates is the corruption signature: refuse with :class:`MissingRegionsError`
    (carrying the report), keep the prior ``current``, and clean staging (#703).
    """

    storage = FsStorage(tmp_path)
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
    good_live = snapshot_dir(tmp_path, community, server)

    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(
        handle, tar_stream({"world/region/r.0.0.mca": healthy_region_bytes()})
    )
    with pytest.raises(MissingRegionsError) as excinfo:
        await storage.commit_snapshot(handle)

    report = excinfo.value.report
    assert len(report.partial_loss) == 1
    assert report.partial_loss[0].directory == Path("world/region")
    assert report.partial_loss[0].lost == ("r.0.1.mca",)

    # current still resolves to the prior snapshot; staging was cleaned.
    assert snapshot_dir(tmp_path, community, server) == good_live
    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {
        "world/region/r.0.0.mca": healthy_region_bytes(),
        "world/region/r.0.1.mca": healthy_region_bytes(),
    }
    incoming = good_live.parent.parent / "incoming"
    assert not incoming.exists() or not any(incoming.iterdir())


async def test_commit_allows_full_dimension_delete(tmp_path: Path) -> None:
    """A publish that removes a WHOLE dimension's regions (legitimate delete) is
    allowed — only a partial loss is the corruption signature (issue #854)."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await _publish(
        storage,
        community,
        server,
        {
            "world/region/r.0.0.mca": healthy_region_bytes(),
            "world/DIM-1/region/r.0.0.mca": healthy_region_bytes(),
            "world/DIM-1/region/r.0.1.mca": healthy_region_bytes(),
        },
    )

    # The Nether (DIM-1) is deleted entirely; the overworld is unchanged.
    after = {"world/region/r.0.0.mca": healthy_region_bytes()}
    await _publish(storage, community, server, after)

    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == after


async def _put_backup(
    storage: FsStorage,
    community: CommunityId,
    server: ServerId,
    files: dict[str, bytes],
) -> BackupKey:
    """Stage a backup archive of ``files`` verbatim, bypassing the create gate.

    ``put_backup`` stores the bytes as-is, so a corrupt region can be parked in a
    backup that the restore gate must later catch (a legacy/uploaded archive).
    """

    async def _stream() -> AsyncIterator[bytes]:
        yield region_targz(files)

    return await storage.put_backup(community, server, _stream())


async def test_restore_corrupt_backup_without_force_refuses_and_keeps_current(
    tmp_path: Path,
) -> None:
    """The restore gate (issue #743): a corrupt backup is refused without ``force``.

    Restoring a backup whose extracted working set is structurally corrupt must
    raise :class:`IntegrityCheckError` (carrying the report), clean the restore
    staging, and leave ``current`` resolving to the prior good snapshot — the
    publish never runs (last-known-good, #703).
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await _publish(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    good_live = snapshot_dir(tmp_path, community, server)
    # The restore gate runs in live mode (issue #923), so use a tear the live rule
    # still catches (a location entry past EOF), not a mere unaligned size.
    key = await _put_backup(
        storage,
        community,
        server,
        {"world/region/r.0.0.mca": mode_invariant_corrupt_region_bytes()},
    )

    with pytest.raises(IntegrityCheckError) as excinfo:
        await storage.restore_backup(community, server, key)
    assert len(excinfo.value.report.corrupt) == 1
    assert excinfo.value.report.corrupt[0].reason is ReasonCode.SECTOR_OUT_OF_BOUNDS

    # current still resolves to the prior good snapshot; restore staging was cleaned.
    assert snapshot_dir(tmp_path, community, server) == good_live
    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"world/region/r.0.0.mca": healthy_region_bytes()}
    incoming = good_live.parent.parent / "incoming"
    assert not incoming.exists() or not any(incoming.iterdir())


async def test_restore_corrupt_backup_with_force_publishes_and_reports_corruption(
    tmp_path: Path,
) -> None:
    """``force=True`` publishes a corrupt backup but still surfaces the corruption.

    The operator override (#703: better a corrupt restore on purpose than no
    restore): the corrupt working set is published to ``current`` anyway, and the
    returned report is non-healthy so the caller can quarantine + audit (#743).
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await _publish(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    # The restore gate runs in live mode (issue #923), so use a tear the live rule
    # still catches (a location entry past EOF), not a mere unaligned size.
    corrupt_region = mode_invariant_corrupt_region_bytes()
    key = await _put_backup(
        storage, community, server, {"world/region/r.0.0.mca": corrupt_region}
    )

    report = await storage.restore_backup(community, server, key, force=True)

    assert not report.healthy
    assert len(report.corrupt) == 1
    # The corrupt backup was published despite the corruption.
    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"world/region/r.0.0.mca": corrupt_region}


async def test_restore_healthy_backup_returns_healthy_report(tmp_path: Path) -> None:
    """A healthy restore publishes as before and returns a healthy report (#743)."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await _publish(storage, community, server, {"server.properties": b"motd=original"})
    key = await _put_backup(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )

    report = await storage.restore_backup(community, server, key)

    assert report.healthy
    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"world/region/r.0.0.mca": healthy_region_bytes()}


async def test_write_snapshot_rejects_symlink_escape_member(tmp_path: Path) -> None:
    from tests.storage.helpers import (
        malicious_tar_with_symlink_escape,
        stream_of,
    )

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    handle = await storage.begin_snapshot(community, server)

    async def _stream() -> AsyncIterator[bytes]:
        async for chunk in stream_of(malicious_tar_with_symlink_escape()):
            yield chunk

    # filter="data" refuses a symlink whose target escapes the extraction root, so
    # extraction raises and no escaping link is created in staging.
    with pytest.raises(Exception):
        await storage.write_snapshot(handle, _stream())
    server_root = (
        tmp_path / "communities" / str(community.value) / "servers" / str(server.value)
    )
    staging_links = list((server_root / "incoming").rglob("escape_link"))
    assert staging_links == []


def _prune_server_root(
    tmp_path: Path, community: CommunityId, server: ServerId
) -> Path:
    return (
        tmp_path / "communities" / str(community.value) / "servers" / str(server.value)
    )


async def test_prune_packs_final_targz_and_drops_the_tree(tmp_path: Path) -> None:
    # fs realization of the DeleteServer reclaim (#777): current/ is packed into
    # final.tar.gz at the server root and the working-set tree is removed.
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    files = {"server.properties": b"motd=hi", "world/level.dat": b"w"}
    await _publish(storage, community, server, files)

    await storage.prune_to_final_snapshot(community, server)

    root = _prune_server_root(tmp_path, community, server)
    final = root / "final.tar.gz"
    assert final.is_file()
    assert read_tar(final.read_bytes()) == files
    # The unpacked working-set tree is gone; backups/ would survive (none here).
    assert not (root / "snapshots").exists()
    assert not (root / "current").exists()
    assert not (root / "versions").exists()


async def test_prune_failclosed_keeps_tree_when_pack_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If packing raises, the working-set tree is left intact and the error
    # propagates — a failed delete never silently loses the latest state (#777).
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await _publish(storage, community, server, {"world/level.dat": b"w"})

    import mc_server_dashboard_api.storage.adapters.fs as fs_mod

    def _boom(directory: Path, archive: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(fs_mod, "_write_tar_gz", _boom)
    with pytest.raises(OSError):
        await storage.prune_to_final_snapshot(community, server)

    root = _prune_server_root(tmp_path, community, server)
    assert not (root / "final.tar.gz").exists()
    # The working set is still published and hydrates unchanged.
    assert read_tar(await drain(storage.open_hydrate_source(community, server))) == {
        "world/level.dat": b"w"
    }


async def test_commit_refuses_when_generation_marker_removed(
    tmp_path: Path,
) -> None:
    # Issue #1704: after prune (or any path that removes the generation marker),
    # _read_generation returns 0. A late commit whose expected_base was the
    # pre-prune generation (>0) must fail with StaleGenerationError, not
    # silently resurrect the snapshot tree. The fix changes the stale check
    # from ``current > expected_base`` to ``current != expected_base``.
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await _publish(storage, community, server, {"f": b"v1"})
    base = await storage.current_generation(community, server)
    assert base >= 1

    # Stage a new snapshot at the pre-removal base...
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream({"f": b"late-upload"}))

    # ...then remove the generation marker (as prune does).
    root = _prune_server_root(tmp_path, community, server)
    marker = root / "generation"
    assert marker.is_file()
    marker.unlink()

    with pytest.raises(StaleGenerationError):
        await storage.commit_snapshot(handle, expected_base=base)


async def test_missing_region_gate_runs_under_server_lock(tmp_path: Path) -> None:
    """TOCTOU regression (issue #921 item 2): check_missing_regions must run inside
    the per-server publish lock, not before it.

    Monkeypatch ``check_missing_regions`` to probe whether the server lock is held
    at call time: a non-blocking ``acquire()`` that returns ``False`` proves the gate
    runs inside the locked section (the same lock ``_publish_and_bump`` takes).
    """

    import mc_server_dashboard_api.storage.adapters.fs as fs_mod
    from mc_server_dashboard_api.storage.integrity.region import check_missing_regions

    storage = FsStorage(tmp_path)
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

    lock = storage._server_lock(community, server)
    lock_was_held = False

    _original = check_missing_regions

    def _probe(staging: Path, prior: Path) -> object:
        nonlocal lock_was_held
        # A non-blocking acquire returns False when the lock is already held by the
        # current thread (threading.Lock is not reentrant), proving this call site is
        # inside the locked section.
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        else:
            lock_was_held = True
        return _original(staging, prior)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(fs_mod, "check_missing_regions", _probe)
    try:
        # Stage a partial-loss snapshot so the gate runs (and raises).
        handle = await storage.begin_snapshot(community, server)
        await storage.write_snapshot(
            handle, tar_stream({"world/region/r.0.0.mca": healthy_region_bytes()})
        )
        with pytest.raises(MissingRegionsError):
            await storage.commit_snapshot(handle)

        assert lock_was_held, (
            "check_missing_regions ran outside the per-server lock — TOCTOU is open"
        )
    finally:
        monkeypatch.undo()
