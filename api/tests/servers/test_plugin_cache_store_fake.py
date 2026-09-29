"""Fake-only plugin-cache behavior outside the shared Port contract."""

from __future__ import annotations

import datetime as dt
import hashlib

from tests.servers.fakes import FakePluginCacheStore

_CONTENT = b"jar-bytes"
_SHA = hashlib.sha256(_CONTENT).hexdigest()
_UNSTAMPED_STORE_TIME = dt.datetime(9999, 1, 1, tzinfo=dt.UTC)


async def test_directly_seeded_blob_reports_far_future_store_time() -> None:
    store = FakePluginCacheStore()
    store.blobs[_SHA] = _CONTENT

    (entry,) = await store.list_entries()

    assert entry.modified_at == _UNSTAMPED_STORE_TIME
