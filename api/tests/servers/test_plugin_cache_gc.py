"""Plugin-cache GC (issue #1332).

Unit tests over in-memory fakes for the PluginCacheStore (list/delete), the
live reference set, and a fixed clock -- no storage, no DB, no network. Cover
the reference-set math (live kept, orphan deleted), the freed-bytes accounting,
and the safety window (a too-young orphan is spared even when unreferenced).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

from mc_server_dashboard_api.servers.application.plugin_cache_gc import (
    GC_SAFETY_WINDOW,
    LivePluginCacheReferences,
    RunPluginCacheGc,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.plugin_cache_store import (
    ByteStream,
    CacheEntry,
    PluginCacheStore,
)

_NOW = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC)


class _FixedClock(Clock):
    def __init__(self, now: dt.datetime) -> None:
        self._now = now

    def now(self) -> dt.datetime:
        return self._now


class _FakeCache(PluginCacheStore):
    def __init__(self, entries: list[CacheEntry]) -> None:
        self._entries = {e.sha256: e for e in entries}
        self.deleted: list[str] = []

    async def has(self, sha256: str) -> bool:
        return sha256 in self._entries

    async def put(self, sha256: str, stream: ByteStream) -> None:  # pragma: no cover
        raise NotImplementedError

    def open(self, sha256: str) -> AsyncIterator[bytes]:  # pragma: no cover
        raise NotImplementedError

    async def list_entries(self) -> list[CacheEntry]:
        return list(self._entries.values())

    async def delete(self, sha256: str) -> None:
        self.deleted.append(sha256)
        self._entries.pop(sha256, None)


class _FakeReferences(LivePluginCacheReferences):
    def __init__(self, keys: set[str]) -> None:
        self._keys = keys
        # Optional: keys to add after the first live() call, simulating a new
        # plugin row committing between the initial snapshot and a re-check.
        self._add_after_first: set[str] = set()
        self._calls = 0

    def add_after_first_call(self, key: str) -> None:
        """Schedule *key* to appear in live() only from the second call onward."""
        self._add_after_first.add(key)

    async def live(self) -> set[str]:
        self._calls += 1
        if self._calls > 1:
            self._keys = self._keys | self._add_after_first
        return self._keys


def _old() -> dt.datetime:
    # Comfortably older than the safety window: never spared by it.
    return _NOW - GC_SAFETY_WINDOW - dt.timedelta(hours=1)


def _entry(sha: str, *, size: int = 10, age: dt.datetime | None = None) -> CacheEntry:
    return CacheEntry(sha256=sha, size_bytes=size, modified_at=age or _old())


def _gc(cache: _FakeCache, refs: _FakeReferences) -> RunPluginCacheGc:
    return RunPluginCacheGc(cache=cache, references=refs, clock=_FixedClock(_NOW))


async def test_deletes_unreferenced_old_blob() -> None:
    cache = _FakeCache([_entry("a" * 64, size=100)])
    refs = _FakeReferences(set())
    result = await _gc(cache, refs)()
    assert cache.deleted == ["a" * 64]
    assert result.scanned == 1
    assert result.deleted == 1
    assert result.freed_bytes == 100


async def test_keeps_referenced_blob() -> None:
    cache = _FakeCache([_entry("a" * 64, size=100)])
    refs = _FakeReferences({"a" * 64})
    result = await _gc(cache, refs)()
    assert cache.deleted == []
    assert result.scanned == 1
    assert result.deleted == 0
    assert result.freed_bytes == 0


async def test_keeps_live_deletes_orphan_in_mixed_cache() -> None:
    live = _entry("a" * 64, size=10)
    orphan = _entry("b" * 64, size=20)
    cache = _FakeCache([live, orphan])
    refs = _FakeReferences({"a" * 64})
    result = await _gc(cache, refs)()
    assert cache.deleted == ["b" * 64]
    assert result.scanned == 2
    assert result.deleted == 1
    assert result.freed_bytes == 20


async def test_safety_window_spares_a_too_young_orphan() -> None:
    # Younger than the window: an in-flight install may have cached it before
    # the plugin row committed.
    young = _entry(
        "c" * 64, size=30, age=_NOW - GC_SAFETY_WINDOW + dt.timedelta(minutes=1)
    )
    cache = _FakeCache([young])
    refs = _FakeReferences(set())
    result = await _gc(cache, refs)()
    assert cache.deleted == []
    assert result.scanned == 1
    assert result.deleted == 0
    assert result.freed_bytes == 0


async def test_safety_window_boundary_is_inclusive_delete() -> None:
    # Exactly at the window edge counts as old enough to delete (>= window).
    at_edge = _entry("d" * 64, size=40, age=_NOW - GC_SAFETY_WINDOW)
    cache = _FakeCache([at_edge])
    refs = _FakeReferences(set())
    result = await _gc(cache, refs)()
    assert cache.deleted == ["d" * 64]
    assert result.deleted == 1
    assert result.freed_bytes == 40


async def test_empty_cache_is_a_noop() -> None:
    cache = _FakeCache([])
    refs = _FakeReferences(set())
    result = await _gc(cache, refs)()
    assert cache.deleted == []
    assert result.scanned == 0
    assert result.deleted == 0
    assert result.freed_bytes == 0


async def test_recheck_before_delete_spares_newly_referenced_blob() -> None:
    """A blob that becomes referenced between the initial snapshot and the delete
    is spared by the pre-delete re-check (issue #1404).

    When a dedup install reuses a cached blob, the old reference may be deleted
    before the new plugin row commits. The blob appears orphaned in the initial
    live() snapshot, but by the time the GC reaches the delete the new row has
    committed. The GC re-checks live() immediately before each delete to catch
    this race.
    """
    sha = "e" * 64
    cache = _FakeCache([_entry(sha, size=50)])
    refs = _FakeReferences(set())  # unreferenced at initial snapshot
    # The new plugin row commits after the first live() call (the initial
    # snapshot) but before the GC attempts the delete (the re-check).
    refs.add_after_first_call(sha)

    result = await _gc(cache, refs)()
    assert cache.deleted == []
    assert result.scanned == 1
    assert result.deleted == 0
