"""Integration tests for the servers FileStore seam over real fs Storage.

Binds :class:`StorageFileStoreAdapter` to a real :class:`FsStorage` (no fakes on
the Storage side) and verifies the at-rest path the file use cases drive: a
versioned edit round-trip (write -> history -> rollback) and the error
translation (missing path -> ServerFileNotFoundError, traversal ->
InvalidFilePathError, FR-FILE-4).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from mc_server_dashboard_api.servers.adapters.file_store import StorageFileStoreAdapter
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidFilePathError,
    ServerFileNotFoundError,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.storage.helpers import publish


def _scope() -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4()


async def _seed(storage: FsStorage, community: uuid.UUID, server: uuid.UUID) -> None:
    await publish(
        storage,
        StorageCommunityId(community),
        StorageServerId(server),
        {"server.properties": b"motd=original"},
    )


async def test_read_returns_published_bytes(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)

    out = await adapter.read_file(
        community_id=CommunityId(community),
        server_id=ServerId(server),
        rel_path="server.properties",
    )
    assert out == b"motd=original"


async def test_edit_history_rollback_round_trip(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)
    cid, sid = CommunityId(community), ServerId(server)

    # Two edits, each retaining the prior version.
    await adapter.write_file(
        community_id=cid,
        server_id=sid,
        rel_path="server.properties",
        content=b"motd=v1",
    )
    await adapter.write_file(
        community_id=cid,
        server_id=sid,
        rel_path="server.properties",
        content=b"motd=v2",
    )

    current = await adapter.read_file(
        community_id=cid, server_id=sid, rel_path="server.properties"
    )
    assert current == b"motd=v2"

    versions = await adapter.list_versions(
        community_id=cid, server_id=sid, rel_path="server.properties"
    )
    # original + v1 retained before each overwrite (newest-first).
    assert len(versions) == 2

    # Roll back to the oldest retained version (the original content).
    await adapter.rollback(
        community_id=cid,
        server_id=sid,
        rel_path="server.properties",
        version_id=versions[-1],
    )
    rolled = await adapter.read_file(
        community_id=cid, server_id=sid, rel_path="server.properties"
    )
    assert rolled == b"motd=original"


async def test_list_dir_browses_root(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await publish(
        storage,
        StorageCommunityId(community),
        StorageServerId(server),
        {"server.properties": b"x", "world/level.dat": b"y"},
    )
    adapter = StorageFileStoreAdapter(storage=storage)

    entries = await adapter.list_dir(
        community_id=CommunityId(community),
        server_id=ServerId(server),
        rel_path=".",
    )
    names = {e.name for e in entries}
    assert names == {"server.properties", "world"}


async def test_read_missing_is_file_not_found(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)

    with pytest.raises(ServerFileNotFoundError):
        await adapter.read_file(
            community_id=CommunityId(community),
            server_id=ServerId(server),
            rel_path="nope.txt",
        )


async def test_read_traversal_is_invalid_path(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)

    for bad in ("../escape", "/etc/passwd", "a/../../escape"):
        with pytest.raises(InvalidFilePathError):
            await adapter.read_file(
                community_id=CommunityId(community),
                server_id=ServerId(server),
                rel_path=bad,
            )
