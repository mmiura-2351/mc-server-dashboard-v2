"""Fidelity of :class:`FakePluginCacheStore` against the real adapter (#2529).

Since #2527 this double is the single description of the ``PluginCacheStore``
Port for every test under ``tests/servers/``, so a property that drifts away
from :class:`ObjectPluginCacheStore` drifts everywhere at once. This asserts
against the fake the same things ``test_plugin_cache_store.py`` asserts against
the adapter — the resource-pack store took the next step and folded both sides
into one parametrized contract (``test_resource_pack_store_contract.py``, #2351).

Pinned here:

- ``open`` on an absent blob does no I/O at the call and raises
  :class:`PluginCacheBlobNotFoundError` on the first iteration, carrying the
  full object key -- the adapter's ``get_object`` timing and message (#2338).
- ``put`` records a store time, which ``list_entries`` then reports; a dedup
  re-put leaves it alone, because the adapter head-checks the content key and
  skips the upload, leaving ``last_modified`` untouched. The GC relies on
  exactly that (``plugin_cache_gc.py`` re-checks live references before
  deleting *because* a dedup put does not refresh the store time).
- ``delete`` absorbs a key that is not there, as ``delete_object`` does.
- A blob seeded straight into ``blobs`` -- the GC tests' idiom -- reports a
  store time no clock can age past, so forgetting the stamp reddens a
  delete-expecting test instead of passing by accident. That fail-loud property
  used to be a wall-clock read, i.e. an unstated environmental assumption
  underneath a safety property (#2529).
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.servers.domain.errors import PluginCacheBlobNotFoundError
from tests.servers.fakes import FakePluginCacheStore

_CONTENT = b"jar-bytes"
_SHA = hashlib.sha256(_CONTENT).hexdigest()
_ABSENT_SHA = "0" * 64


async def _stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def test_open_of_absent_blob_raises_not_found_while_streaming() -> None:
    store = FakePluginCacheStore()

    # The adapter's open() performs no I/O itself: it hands back a generator and
    # the miss surfaces on the first chunk. The fake must fail at the same point,
    # so a consumer sees the error where production puts it (issue #2338).
    stream = store.open(_ABSENT_SHA)

    with pytest.raises(PluginCacheBlobNotFoundError):
        assert [chunk async for chunk in stream]


async def test_open_of_absent_blob_reports_the_full_object_key() -> None:
    # The adapter raises with the key it missed, ``plugin-cache/<sha256>``.
    store = FakePluginCacheStore()

    # Opened outside the ``raises`` block for the same reason as above: taken
    # inside it, this test would pass on an eager raise too, and the laziness
    # pin would rest entirely on its neighbour.
    stream = store.open(_ABSENT_SHA)

    with pytest.raises(PluginCacheBlobNotFoundError) as excinfo:
        assert [chunk async for chunk in stream]

    assert f"plugin-cache/{_ABSENT_SHA}" in str(excinfo.value)


async def test_put_records_the_store_time() -> None:
    # The adapter uploads the blob, so the object's ``last_modified`` -- what
    # ``list_entries`` reports -- is the moment of the put.
    store = FakePluginCacheStore()
    before = dt.datetime.now(dt.UTC)

    await store.put(_SHA, _stream(_CONTENT))

    after = dt.datetime.now(dt.UTC)
    (entry,) = await store.list_entries()
    assert before <= entry.modified_at <= after


async def test_dedup_put_preserves_the_original_store_time() -> None:
    """The fake keeps the first stamp, matching the adapter's dedup skip.

    Two things ride on this. The adapter head-checks the content key and skips
    the upload when the blob is already cached, so a re-put leaves
    ``last_modified`` where it was -- and separately, the double has to be
    deterministic about it: a GC test that seeds a store time and then re-puts
    the same bytes has to keep reading the time it seeded, otherwise the blob
    silently becomes young and the test stops asserting what it says it asserts.
    (The sibling ``FakeJarPool`` has only the second reason -- its two backends
    disagree -- so its version of this test claims only the convention.)
    """
    store = FakePluginCacheStore()
    await store.put(_SHA, _stream(_CONTENT))
    stored_at = dt.datetime(2026, 6, 20, 12, 0, tzinfo=dt.UTC)
    store.modified_at[_SHA] = stored_at

    await store.put(_SHA, _stream(_CONTENT))

    (entry,) = await store.list_entries()
    assert entry.modified_at == stored_at


async def test_put_after_delete_records_a_fresh_store_time() -> None:
    # Deleting drops the object, so the next put uploads again and the blob gets
    # a new ``last_modified`` -- it must not inherit the deleted blob's.
    store = FakePluginCacheStore()
    await store.put(_SHA, _stream(_CONTENT))
    store.modified_at[_SHA] = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    await store.delete(_SHA)

    before = dt.datetime.now(dt.UTC)
    await store.put(_SHA, _stream(_CONTENT))

    (entry,) = await store.list_entries()
    assert entry.modified_at >= before


async def test_delete_of_absent_blob_is_idempotent() -> None:
    # ``delete_object`` does not fault on a missing key, and the Port promises
    # the same ("no error if absent"), so the GC may re-delete a swept blob.
    store = FakePluginCacheStore()

    await store.delete(_ABSENT_SHA)

    assert store.deleted == [_ABSENT_SHA]


async def test_unstamped_blob_reports_a_store_time_no_clock_can_age() -> None:
    """A blob seeded without a stamp is spared by the GC under *any* clock.

    Reading the host clock made that hold only while the clock ran later than
    the test's fixed ``now`` minus the safety window -- true in practice, but an
    environmental assumption propping up a fail-loud property (#2529). The store
    time is now a fixed sentinel: stable across calls (so it is not the moment
    ``list_entries`` ran) and beyond any clock a test would use.
    """
    store = FakePluginCacheStore()
    store.blobs[_SHA] = _CONTENT

    (first,) = await store.list_entries()
    (second,) = await store.list_entries()

    assert first.modified_at == second.modified_at
    assert first.modified_at > dt.datetime(3000, 1, 1, tzinfo=dt.UTC)
