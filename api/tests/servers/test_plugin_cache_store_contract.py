"""The PluginCacheStore contract, shared by the use-case fake and object adapter."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.servers.adapters.plugin_cache_store import (
    ObjectPluginCacheStore,
)
from mc_server_dashboard_api.servers.domain.errors import PluginCacheBlobNotFoundError
from mc_server_dashboard_api.servers.domain.plugin_cache_store import PluginCacheStore
from tests.servers.fakes import FakePluginCacheStore
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory
from tests.store_clock import StoreClock

_CONTENT = b"jar-bytes"
_SHA = hashlib.sha256(_CONTENT).hexdigest()
_MISSING = "0" * 64


async def _stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


@pytest.fixture(params=["fake", "object"])
def cache(request: pytest.FixtureRequest) -> tuple[PluginCacheStore, StoreClock]:
    clock = StoreClock()
    if request.param == "fake":
        return FakePluginCacheStore(clock=clock), clock
    return ObjectPluginCacheStore(fake_s3_factory(FakeS3Store(clock=clock))), clock


async def test_put_round_trips_bytes_and_reports_store_time(
    cache: tuple[PluginCacheStore, StoreClock],
) -> None:
    store, clock = cache

    await store.put(_SHA, _stream(_CONTENT))

    assert b"".join([chunk async for chunk in store.open(_SHA)]) == _CONTENT
    (entry,) = await store.list_entries()
    assert (entry.sha256, entry.size_bytes, entry.modified_at) == (
        _SHA,
        len(_CONTENT),
        clock.now,
    )


async def test_reput_preserves_the_first_store_time(
    cache: tuple[PluginCacheStore, StoreClock],
) -> None:
    store, clock = cache
    await store.put(_SHA, _stream(_CONTENT))
    first_store_time = clock.now
    clock.advance()

    await store.put(_SHA, _stream(_CONTENT))

    (entry,) = await store.list_entries()
    assert entry.modified_at == first_store_time


async def test_put_after_delete_has_a_new_store_time(
    cache: tuple[PluginCacheStore, StoreClock],
) -> None:
    store, clock = cache
    await store.put(_SHA, _stream(_CONTENT))
    await store.delete(_SHA)
    clock.advance()

    await store.put(_SHA, _stream(_CONTENT))

    (entry,) = await store.list_entries()
    assert entry.modified_at == clock.now


async def test_missing_blob_fails_on_iteration_with_its_object_key(
    cache: tuple[PluginCacheStore, StoreClock],
) -> None:
    store, _ = cache
    stream = store.open(_MISSING)

    with pytest.raises(PluginCacheBlobNotFoundError, match=f"plugin-cache/{_MISSING}"):
        _ = [chunk async for chunk in stream]


async def test_delete_removes_blob_and_is_idempotent(
    cache: tuple[PluginCacheStore, StoreClock],
) -> None:
    store, _ = cache
    await store.put(_SHA, _stream(_CONTENT))

    await store.delete(_SHA)
    await store.delete(_SHA)
    await store.delete(_MISSING)

    assert await store.list_entries() == []
    with pytest.raises(PluginCacheBlobNotFoundError):
        _ = [chunk async for chunk in store.open(_SHA)]
