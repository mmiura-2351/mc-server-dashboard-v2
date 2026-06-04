"""Snapshot ingest + atomic publish + layout conformance (STORAGE.md Sections 2-4).

A snapshot streams into staging, publishes atomically by a ``current`` symlink
flip, and the on-disk layout matches Section 2. ``current`` always resolves to a
complete snapshot; superseded snapshots are reclaimed; staging stays out of
``current``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.errors import (
    IncompleteTransferError,
    NotFoundError,
    SnapshotHandleError,
)
from mc_server_dashboard_api.storage.domain.port import SnapshotHandle
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    ServerId,
)
from tests.storage.helpers import (
    drain,
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
) -> SnapshotHandle:
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(files))
    await storage.commit_snapshot(handle)
    return handle


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


async def test_hydrate_returns_published_working_set(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    files = {"server.properties": b"a=b", "world/level.dat": b"world-bytes"}
    await _publish(storage, community, server, files)

    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == files


async def test_hydrate_before_any_publish_is_not_found(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    with pytest.raises(NotFoundError):
        await drain(storage.open_hydrate_source(community, server))


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
    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == {"f": b"live"}


async def test_abort_is_idempotent(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    handle = await storage.begin_snapshot(community, server)
    await storage.abort_snapshot(handle)
    await storage.abort_snapshot(handle)  # no raise


async def test_commit_after_commit_rejects_reused_handle(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    handle = await _publish(storage, community, server, {"f": b"v1"})
    with pytest.raises((SnapshotHandleError, IncompleteTransferError)):
        await storage.commit_snapshot(handle)


async def test_write_snapshot_sandboxes_malicious_members(tmp_path: Path) -> None:
    from collections.abc import AsyncIterator

    from tests.storage.helpers import malicious_tar_with_escape, stream_of

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    handle = await storage.begin_snapshot(community, server)

    async def _stream() -> AsyncIterator[bytes]:
        async for chunk in stream_of(malicious_tar_with_escape()):
            yield chunk

    # filter="data" refuses the ../ member, so extraction raises rather than
    # writing outside staging; nothing escapes the server root.
    with pytest.raises(Exception):
        await storage.write_snapshot(handle, _stream())
    assert not (tmp_path / "escape.txt").exists()
