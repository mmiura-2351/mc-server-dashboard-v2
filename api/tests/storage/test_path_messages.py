"""A refusal names the caller's path escaped, on both backends (issue #3957).

A path comes from the user, and a refusal's message reaches a traceback if the
error ever escapes unhandled. ``RelPath`` keeps C0 and DEL out, but not a C1
character such as NEL (U+0085), which some readers take as a line break. So
every not-found message, and the write-onto-a-directory refusal, names the path
``repr``'d, as the object client names its keys (issue #3922).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.errors import (
    NotFoundError,
    PathTraversalError,
)
from mc_server_dashboard_api.storage.domain.value_objects import RelPath
from tests.storage.conftest import StorageHarness
from tests.storage.helpers import drain, new_scope, publish, snapshot_dir

# A name RelPath accepts that a raw message would carry unescaped.
_NAME = "a\x85b"


async def test_read_file_names_the_path_escaped(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    with pytest.raises(NotFoundError) as excinfo:
        await harness.storage.read_file(community, server, RelPath(_NAME))
    assert str(excinfo.value) == f"file not found: {_NAME!r}"


async def test_open_file_stream_names_the_path_escaped(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    with pytest.raises(NotFoundError) as excinfo:
        await drain(harness.storage.open_file_stream(community, server, RelPath(_NAME)))
    assert str(excinfo.value) == f"file not found: {_NAME!r}"


async def test_list_dir_names_the_path_escaped(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    with pytest.raises(NotFoundError) as excinfo:
        await harness.storage.list_dir(community, server, RelPath(_NAME))
    assert str(excinfo.value) == f"directory not found: {_NAME!r}"


async def test_write_onto_a_directory_names_the_path_escaped(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {f"{_NAME}/f": b"x"})
    with pytest.raises(PathTraversalError) as excinfo:
        await harness.storage.write_file(community, server, RelPath(_NAME), b"y")
    assert str(excinfo.value) == f"rel_path names a directory, not a file: {_NAME!r}"


async def test_delete_file_names_the_path_escaped(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    with pytest.raises(NotFoundError) as excinfo:
        await harness.storage.delete_file(community, server, RelPath(_NAME))
    assert str(excinfo.value) == f"file not found: {_NAME!r}"


async def test_delete_dir_names_the_path_escaped(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    with pytest.raises(NotFoundError) as excinfo:
        await harness.storage.delete_dir(community, server, RelPath(_NAME))
    assert str(excinfo.value) == f"directory not found: {_NAME!r}"


async def test_rename_file_names_the_source_escaped(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    with pytest.raises(NotFoundError) as excinfo:
        await harness.storage.rename_file(
            community, server, RelPath(_NAME), RelPath("g")
        )
    assert str(excinfo.value) == f"file not found: {_NAME!r}"


async def test_rename_dir_names_the_source_escaped(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    with pytest.raises(NotFoundError) as excinfo:
        await harness.storage.rename_dir(
            community, server, RelPath(_NAME), RelPath("g")
        )
    assert str(excinfo.value) == f"directory not found: {_NAME!r}"


async def test_view_with_no_snapshot_names_the_path_escaped(
    harness: StorageHarness,
) -> None:
    community, server = new_scope()
    async with harness.storage.open_working_set_view(community, server) as view:
        with pytest.raises(NotFoundError) as listed:
            await view.list_dir(RelPath(_NAME))
        with pytest.raises(NotFoundError) as streamed:
            await drain(view.open_file_stream(RelPath(_NAME)))
    assert str(listed.value) == f"directory not found: {_NAME!r}"
    assert str(streamed.value) == f"file not found: {_NAME!r}"


async def test_view_names_a_missing_path_escaped(harness: StorageHarness) -> None:
    community, server = new_scope()
    await harness.publish(community, server, {"f": b"x"})
    async with harness.storage.open_working_set_view(community, server) as view:
        with pytest.raises(NotFoundError) as listed:
            await view.list_dir(RelPath(_NAME))
        with pytest.raises(NotFoundError) as streamed:
            await drain(view.open_file_stream(RelPath(_NAME)))
    assert str(listed.value) == f"directory not found: {_NAME!r}"
    assert str(streamed.value) == f"file not found: {_NAME!r}"


async def test_fs_delete_dir_of_a_leaf_symlink_names_the_path_escaped(
    tmp_path: Path,
) -> None:
    # Only fs has symlinks: a leaf link to a directory is a miss, not a subtree.
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"d/f": b"x"})
    live = snapshot_dir(tmp_path, community, server)
    os.symlink(live / "d", live / _NAME)
    with pytest.raises(NotFoundError) as excinfo:
        await storage.delete_dir(community, server, RelPath(_NAME))
    assert str(excinfo.value) == f"directory not found: {_NAME!r}"
