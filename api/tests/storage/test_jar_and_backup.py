"""JAR content-addressing and backup archive create/list/restore/delete.

STORAGE.md Sections 3.2, 3.3.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.errors import NotFoundError
from mc_server_dashboard_api.storage.domain.value_objects import BackupKey, JarKey
from tests.storage.helpers import drain, new_scope, publish, read_tar, stream_of


async def test_put_jar_returns_sha256_content_key(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    data = b"jar-bytes-here"
    key = await storage.put_jar(stream_of(data))
    assert key == JarKey(hashlib.sha256(data).hexdigest())


async def test_put_jar_is_idempotent_and_dedupes(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    data = b"the-same-jar"
    k1 = await storage.put_jar(stream_of(data))
    k2 = await storage.put_jar(stream_of(data))
    assert k1 == k2
    jars = list((tmp_path / "jars").iterdir())
    assert [p.name for p in jars] == [f"{k1.sha256}.jar"]  # stored once


async def test_jar_round_trip(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    data = b"x" * (3 * 1024 * 1024 + 17)  # multi-chunk
    key = await storage.put_jar(stream_of(data, chunk=4096))
    assert await storage.has_jar(key) is True
    assert await drain(storage.open_jar(key)) == data


async def test_has_jar_false_when_absent(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    assert await storage.has_jar(JarKey("a" * 64)) is False


async def test_open_missing_jar_is_not_found(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    with pytest.raises(NotFoundError):
        await drain(storage.open_jar(JarKey("b" * 64)))


async def test_backup_create_list_restore_round_trip(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    original = {"server.properties": b"k=v", "world/level.dat": b"world"}
    await publish(storage, community, server, original)

    key = await storage.create_backup_from_current(community, server)
    assert key in await storage.list_backups(community, server)

    # Mutate current, then restore the backup -> current republished atomically.
    await publish(storage, community, server, {"server.properties": b"changed"})
    await storage.restore_backup(community, server, key)

    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == original


async def test_backup_from_current_without_publish_is_not_found(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    with pytest.raises(NotFoundError):
        await storage.create_backup_from_current(community, server)


async def test_restore_unknown_backup_is_not_found(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"x"})
    with pytest.raises(NotFoundError):
        await storage.restore_backup(community, server, BackupKey("nope"))


async def test_delete_backup_is_idempotent(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"x"})
    key = await storage.create_backup_from_current(community, server)

    await storage.delete_backup(community, server, key)
    assert key not in await storage.list_backups(community, server)
    await storage.delete_backup(community, server, key)  # no raise on second
