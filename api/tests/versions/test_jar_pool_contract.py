"""The JarPool contract, shared by its fake and the storage-backed adapter."""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import pytest

from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.versions.adapters.storage_jar_pool import StorageJarPool
from mc_server_dashboard_api.versions.domain.jar_pool import JarPool, PoolStats
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory
from tests.store_clock import StoreClock
from tests.versions.fakes import FakeJarPool

_JAR = b"jar-bytes"
_SHA = hashlib.sha256(_JAR).hexdigest()
_MISSING = "a" * 64


@pytest.fixture(params=["fake", "fs", "object"])
def pool(
    request: pytest.FixtureRequest, tmp_path: Path
) -> tuple[JarPool, StoreClock, bool]:
    clock = StoreClock()
    if request.param == "fake":
        return FakeJarPool(clock=clock), clock, True
    if request.param == "fs":
        return StorageJarPool(FsStorage(tmp_path)), clock, False
    return (
        StorageJarPool(ObjectStorage(fake_s3_factory(FakeS3Store(clock=clock)))),
        clock,
        True,
    )


async def test_put_lists_content_key_size_and_store_time(
    pool: tuple[JarPool, StoreClock, bool],
) -> None:
    jars, clock, controlled = pool
    assert await jars.list_entries() == []

    key = await jars.put(_JAR)

    assert key == _SHA
    assert await jars.has(key)
    assert await jars.stats() == PoolStats(count=1, total_bytes=len(_JAR))
    (entry,) = await jars.list_entries()
    assert (entry.sha256, entry.size_bytes) == (_SHA, len(_JAR))
    assert entry.modified_at.tzinfo is not None
    assert entry.modified_at < dt.datetime(3000, 1, 1, tzinfo=dt.UTC)
    if controlled:
        assert entry.modified_at == clock.now


async def test_identical_bytes_share_one_content_key(
    pool: tuple[JarPool, StoreClock, bool],
) -> None:
    jars, _, _ = pool

    assert await jars.put(_JAR) == await jars.put(_JAR)

    assert await jars.stats() == PoolStats(count=1, total_bytes=len(_JAR))
    assert [entry.sha256 for entry in await jars.list_entries()] == [_SHA]


async def test_delete_then_put_restores_one_entry(
    pool: tuple[JarPool, StoreClock, bool],
) -> None:
    jars, clock, controlled = pool
    key = await jars.put(_JAR)
    await jars.delete(key)
    clock.advance()

    assert await jars.put(_JAR) == key

    (entry,) = await jars.list_entries()
    assert entry.sha256 == key
    assert entry.modified_at < dt.datetime(3000, 1, 1, tzinfo=dt.UTC)
    if controlled:
        assert entry.modified_at == clock.now


async def test_missing_and_deleted_jar_are_absent(
    pool: tuple[JarPool, StoreClock, bool],
) -> None:
    jars, _, _ = pool
    assert await jars.has(_MISSING) is False

    await jars.delete(_MISSING)
    key = await jars.put(_JAR)
    await jars.delete(key)
    await jars.delete(key)

    assert await jars.has(key) is False
    assert await jars.list_entries() == []
    assert await jars.stats() == PoolStats(count=0, total_bytes=0)
