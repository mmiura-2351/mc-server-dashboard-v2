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
from mc_server_dashboard_api.storage.domain.errors import IntegrityCheckError
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    ServerId,
)
from mc_server_dashboard_api.storage.integrity.region import ReasonCode
from tests.storage.helpers import (
    corrupt_region_bytes,
    drain,
    healthy_region_bytes,
    new_scope,
    read_tar,
    snapshot_dir,
    tar_stream,
)


async def _publish(
    storage: FsStorage,
    community: CommunityId,
    server: ServerId,
    files: dict[str, bytes],
) -> None:
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(files))
    await storage.commit_snapshot(handle)


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
