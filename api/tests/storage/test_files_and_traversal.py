"""Authoritative-copy file ops, version retention, and traversal containment.

STORAGE.md Sections 3.4, 3.5, 4.4, 5, 6.
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
from tests.storage.helpers import new_scope, publish, snapshot_dir


async def test_read_file_returns_published_content(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"server.properties": b"motd=hello"})
    assert (
        await storage.read_file(community, server, RelPath("server.properties"))
        == b"motd=hello"
    )


async def test_read_missing_file_is_not_found(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"x"})
    with pytest.raises(NotFoundError):
        await storage.read_file(community, server, RelPath("missing.txt"))


async def test_list_dir_lists_entries(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(
        storage,
        community,
        server,
        {"world/level.dat": b"abc", "server.properties": b"k=v"},
    )
    root_entries = await storage.list_dir(community, server, RelPath("."))
    names = {(e.name, e.is_dir) for e in root_entries}
    assert ("world", True) in names
    assert ("server.properties", False) in names
    props = next(e for e in root_entries if e.name == "server.properties")
    assert props.size == 3


async def test_write_file_overwrites_and_retains_prior_version(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"cfg": b"v1"})

    await storage.write_file(community, server, RelPath("cfg"), b"v2")
    assert await storage.read_file(community, server, RelPath("cfg")) == b"v2"

    versions = await storage.list_file_versions(community, server, RelPath("cfg"))
    assert len(versions) == 1
    assert (
        await storage.read_file_version(community, server, RelPath("cfg"), versions[0])
        == b"v1"
    )


async def test_write_file_creates_new_file_without_version(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"existing": b"x"})

    await storage.write_file(community, server, RelPath("new.txt"), b"fresh")
    assert await storage.read_file(community, server, RelPath("new.txt")) == b"fresh"
    assert await storage.list_file_versions(community, server, RelPath("new.txt")) == []


async def test_version_retention_is_count_bounded(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path, version_retention=3)
    community, server = new_scope()
    await publish(storage, community, server, {"cfg": b"v0"})
    for i in range(1, 8):
        await storage.write_file(community, server, RelPath("cfg"), f"v{i}".encode())

    versions = await storage.list_file_versions(community, server, RelPath("cfg"))
    assert len(versions) == 3  # bounded; oldest pruned (Section 5)
    # Newest-first: the most recent retained prior content is v6 (current is v7).
    contents = [
        await storage.read_file_version(community, server, RelPath("cfg"), v)
        for v in versions
    ]
    assert contents[0] == b"v6"
    assert b"v0" not in contents and b"v3" not in contents


async def test_rollback_restores_and_is_reversible(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"cfg": b"first"})
    await storage.write_file(community, server, RelPath("cfg"), b"second")

    versions = await storage.list_file_versions(community, server, RelPath("cfg"))
    await storage.rollback_file(community, server, RelPath("cfg"), versions[0])
    assert await storage.read_file(community, server, RelPath("cfg")) == b"first"

    # Reversible: the pre-rollback content ("second") is itself now retained.
    versions_after = await storage.list_file_versions(community, server, RelPath("cfg"))
    latest = await storage.read_file_version(
        community, server, RelPath("cfg"), versions_after[0]
    )
    assert latest == b"second"


async def test_read_rejects_symlink_escape(tmp_path: Path) -> None:
    """A symlink inside current/ pointing outside the root is refused (Section 6)."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"x"})

    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"top-secret")
    live = snapshot_dir(tmp_path, community, server)
    os.symlink(secret, live / "escape")

    with pytest.raises(PathTraversalError):
        await storage.read_file(community, server, RelPath("escape"))


async def test_list_dir_rejects_symlink_escape(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"x"})

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    live = snapshot_dir(tmp_path, community, server)
    os.symlink(outside_dir, live / "escape_dir")

    with pytest.raises(PathTraversalError):
        await storage.list_dir(community, server, RelPath("escape_dir"))


async def test_internal_symlink_within_root_is_allowed(tmp_path: Path) -> None:
    """A symlink that resolves to a location still inside current/ is fine."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real/data": b"inside"})

    live = snapshot_dir(tmp_path, community, server)
    os.symlink(live / "real", live / "alias")
    assert (
        await storage.read_file(community, server, RelPath("alias/data")) == b"inside"
    )
