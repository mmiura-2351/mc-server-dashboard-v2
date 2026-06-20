"""Adapter test for the object-store plugin cache (issue #1306).

Pins the content-addressed dedup behaviour of :class:`ObjectPluginCacheStore`
against the in-memory S3 stub (no real cloud): a blob lands under
``plugin-cache/<sha256>``, a second ``put`` of identical content skips the
upload, and ``open`` round-trips the bytes.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.servers.adapters.plugin_cache_store import (
    ObjectPluginCacheStore,
)
from mc_server_dashboard_api.storage.domain.errors import NotFoundError
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory


async def _stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def test_put_stores_blob_under_content_key() -> None:
    store = FakeS3Store()
    cache = ObjectPluginCacheStore(fake_s3_factory(store))
    content = b"jar-bytes"
    sha256 = hashlib.sha256(content).hexdigest()

    await cache.put(sha256, _stream(content))

    assert f"plugin-cache/{sha256}" in store.objects
    assert await cache.has(sha256) is True


async def test_put_dedups_identical_content() -> None:
    """A second put of identical content skips the upload (dedup-on-ingest)."""
    store = FakeS3Store()
    cache = ObjectPluginCacheStore(fake_s3_factory(store))
    content = b"identical-bytes"
    sha256 = hashlib.sha256(content).hexdigest()
    key = f"plugin-cache/{sha256}"

    await cache.put(sha256, _stream(content))
    assert store.upload_calls[key] == 1

    await cache.put(sha256, _stream(content))
    # The second put head-checked the existing key and skipped the upload, so the
    # stub's per-key upload tally stays at one. (multipart_parts can't catch a
    # re-upload of identical content — it's overwritten with the same count.)
    assert store.upload_calls[key] == 1


async def test_open_round_trips_bytes() -> None:
    store = FakeS3Store()
    cache = ObjectPluginCacheStore(fake_s3_factory(store))
    content = b"round-trip-bytes"
    sha256 = hashlib.sha256(content).hexdigest()
    await cache.put(sha256, _stream(content))

    out = b"".join([chunk async for chunk in cache.open(sha256)])
    assert out == content


async def test_open_missing_raises_not_found() -> None:
    store = FakeS3Store()
    cache = ObjectPluginCacheStore(fake_s3_factory(store))
    with pytest.raises(NotFoundError):
        _ = [chunk async for chunk in cache.open("0" * 64)]


async def test_list_entries_returns_cached_blobs() -> None:
    store = FakeS3Store()
    cache = ObjectPluginCacheStore(fake_s3_factory(store))
    content_a = b"blob-a"
    content_b = b"blob-b"
    sha_a = hashlib.sha256(content_a).hexdigest()
    sha_b = hashlib.sha256(content_b).hexdigest()
    await cache.put(sha_a, _stream(content_a))
    await cache.put(sha_b, _stream(content_b))

    entries = await cache.list_entries()
    keys = {e.sha256 for e in entries}
    assert keys == {sha_a, sha_b}
    for e in entries:
        assert e.size_bytes > 0
        assert e.modified_at is not None


async def test_list_entries_empty_cache() -> None:
    store = FakeS3Store()
    cache = ObjectPluginCacheStore(fake_s3_factory(store))
    entries = await cache.list_entries()
    assert entries == []


async def test_delete_removes_blob() -> None:
    store = FakeS3Store()
    cache = ObjectPluginCacheStore(fake_s3_factory(store))
    content = b"to-delete"
    sha = hashlib.sha256(content).hexdigest()
    await cache.put(sha, _stream(content))
    assert await cache.has(sha) is True

    await cache.delete(sha)
    assert await cache.has(sha) is False


async def test_delete_absent_is_idempotent() -> None:
    store = FakeS3Store()
    cache = ObjectPluginCacheStore(fake_s3_factory(store))
    # Should not raise on a missing key.
    await cache.delete("0" * 64)
