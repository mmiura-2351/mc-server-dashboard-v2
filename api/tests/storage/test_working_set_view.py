"""Working-set view: snapshot pinning across export/download walks (issue #1966).

The ``WorkingSetView`` pins one snapshot for the life of its context manager via
an active-reader lease. A concurrent publish cannot change what the view reads,
so an export/download that walks the tree mid-restore gets a consistent zip.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from mc_server_dashboard_api.storage.domain.errors import NotFoundError
from mc_server_dashboard_api.storage.domain.value_objects import RelPath
from tests.storage.conftest import BACKENDS, StorageHarness, build_harness
from tests.storage.helpers import drain, new_scope


@pytest.fixture(params=BACKENDS)
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> StorageHarness:
    return build_harness(request.param, tmp_path)


# 13 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(13 * 10 + 30)
async def test_view_pins_snapshot_across_concurrent_publish(
    harness: StorageHarness,
) -> None:
    """A view opened before a publish still reads the OLD tree."""
    community, server = new_scope()
    await harness.publish(community, server, {"a.txt": b"OLD_A", "b.txt": b"OLD_B"})

    async with harness.storage.open_working_set_view(community, server) as view:
        # Publish a DIFFERENT tree while the view is held.
        await harness.publish(community, server, {"c.txt": b"NEW_C"})

        # The view still sees the OLD tree.
        entries = await view.list_dir(RelPath("."))
        names = sorted(e.name for e in entries)
        assert names == ["a.txt", "b.txt"]

        # File reads through the view yield OLD content.
        stream = view.open_file_stream(RelPath("a.txt"))
        content = b"".join([chunk async for chunk in stream])
        assert content == b"OLD_A"


# 12 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(12 * 10 + 30)
async def test_view_lease_survives_publish_and_sweep(
    harness: StorageHarness,
) -> None:
    """While the view is open, the old snapshot survives publish + sweep."""
    community, server = new_scope()
    await harness.publish(community, server, {"f.txt": b"CONTENT"})

    async with harness.storage.open_working_set_view(community, server) as view:
        # Publish + sweep: the OLD snapshot would be reclaimed without the lease.
        await harness.publish(community, server, {"g.txt": b"NEW"})
        await harness.sweep()

        # The view still works fine — lease protects the old snapshot.
        entries = await view.list_dir(RelPath("."))
        assert any(e.name == "f.txt" for e in entries)
        stream = view.open_file_stream(RelPath("f.txt"))
        content = b"".join([chunk async for chunk in stream])
        assert content == b"CONTENT"

    # After the view is closed, the next sweep reclaims the old snapshot.
    await harness.sweep()
    # Verify the new snapshot is the live one.
    entries = await harness.storage.list_dir(community, server, RelPath("."))
    names = [e.name for e in entries]
    assert "g.txt" in names
    assert "f.txt" not in names


# 12 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(12 * 10 + 30)
async def test_a_view_stream_outliving_its_view_keeps_its_snapshot_leased(
    harness: StorageHarness,
) -> None:
    """A stream left open past its view's exit pins the snapshot until it closes.

    The stream takes a lease of its own on its first iteration (issues #3618,
    #3670), so the view's exit does not leave it reading an unleased snapshot.
    """
    community, server = new_scope()
    await harness.publish(community, server, {"f.txt": b"CONTENT"})
    storage = cast(Any, harness.storage)

    async with harness.storage.open_working_set_view(community, server) as view:
        stream = view.open_file_stream(RelPath("f.txt"))
        first = await stream.__anext__()
        [pinned] = storage._leases

    # Publish + sweep: the old snapshot is reclaimed unless something leases it.
    await harness.publish(community, server, {"g.txt": b"NEW"})
    await harness.sweep()
    assert storage._is_leased(pinned)

    assert first + await drain(stream) == b"CONTENT"
    assert not storage._is_leased(pinned)


async def test_view_walks_subdirectory(harness: StorageHarness) -> None:
    """The view correctly lists and reads files inside subdirectories."""
    community, server = new_scope()
    await harness.publish(
        community, server, {"dir/sub.txt": b"SUB", "root.txt": b"ROOT"}
    )

    async with harness.storage.open_working_set_view(community, server) as view:
        # List root — should see both dir and root.txt.
        root_entries = await view.list_dir(RelPath("."))
        root_names = sorted(e.name for e in root_entries)
        assert "dir" in root_names
        assert "root.txt" in root_names

        # List subdir.
        sub_entries = await view.list_dir(RelPath("dir"))
        assert len(sub_entries) == 1
        assert sub_entries[0].name == "sub.txt"

        # Read file in subdir.
        content = b"".join(
            [chunk async for chunk in view.open_file_stream(RelPath("dir/sub.txt"))]
        )
        assert content == b"SUB"


async def test_view_file_stream_on_a_directory_is_not_found(
    harness: StorageHarness,
) -> None:
    """Through the view too, a path naming no readable FILE is a miss."""
    community, server = new_scope()
    await harness.publish(community, server, {"world/level.dat": b"x"})

    async with harness.storage.open_working_set_view(community, server) as view:
        with pytest.raises(NotFoundError):
            async for _ in view.open_file_stream(RelPath("world")):
                pass


async def test_view_unpublished_server_returns_empty(
    harness: StorageHarness,
) -> None:
    """A view on an unpublished server lists root as empty and raises on file reads."""
    community, server = new_scope()

    async with harness.storage.open_working_set_view(community, server) as view:
        entries = await view.list_dir(RelPath("."))
        assert entries == []

        with pytest.raises(NotFoundError):
            await view.list_dir(RelPath("subdir"))

        with pytest.raises(NotFoundError):
            # open_file_stream may raise immediately or on first iteration.
            stream = view.open_file_stream(RelPath("missing.txt"))
            await stream.__anext__()


# 15 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(15 * 10 + 30)
async def test_concurrent_restore_does_not_tear_export_walk(
    harness: StorageHarness,
) -> None:
    """Regression: publish tree with subdir, start export view, restore different tree,
    drain — assert all members match pre-restore tree."""
    community, server = new_scope()
    original = {
        "world/region.dat": b"REGION_OLD",
        "world/level.dat": b"LEVEL_OLD",
        "server.properties": b"PROPS_OLD",
    }
    await harness.publish(community, server, original)

    async with harness.storage.open_working_set_view(community, server) as view:
        # Simulate a restore/publish mid-stream — tree changes completely.
        await harness.publish(
            community, server, {"totally_different.txt": b"NEW_CONTENT"}
        )

        # Walk the view — should see the ORIGINAL tree only.
        collected: dict[str, bytes] = {}
        root_entries = await view.list_dir(RelPath("."))
        stack = [("", root_entries)]
        while stack:
            prefix, entries = stack.pop()
            for entry in entries:
                path = f"{prefix}/{entry.name}" if prefix else entry.name
                if entry.is_dir:
                    sub = await view.list_dir(RelPath(path))
                    stack.append((path, sub))
                else:
                    data = b"".join(
                        [chunk async for chunk in view.open_file_stream(RelPath(path))]
                    )
                    collected[path] = data

        assert collected == original
