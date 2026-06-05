"""Reference-counted JAR-pool GC (D4, issue #293).

Unit tests over in-memory fakes for the JarPool (list/delete), the live
reference set, and a fixed clock — no storage, no DB, no network. Cover the
reference-set math (live kept, orphan deleted), the freed-bytes accounting, and
the safety window (a too-young orphan is spared even when unreferenced).
"""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.versions.application.jar_gc import (
    GC_SAFETY_WINDOW,
    RunJarPoolGc,
)
from mc_server_dashboard_api.versions.domain.clock import Clock
from mc_server_dashboard_api.versions.domain.jar_pool import (
    JarPool,
    PoolEntry,
    PoolStats,
)
from mc_server_dashboard_api.versions.domain.jar_references import LiveJarReferences

_NOW = dt.datetime(2026, 6, 5, 12, 0, 0, tzinfo=dt.UTC)


class _FixedClock(Clock):
    def __init__(self, now: dt.datetime) -> None:
        self._now = now

    def now(self) -> dt.datetime:
        return self._now


class _FakePool(JarPool):
    def __init__(self, entries: list[PoolEntry]) -> None:
        self._entries = {e.sha256: e for e in entries}
        self.deleted: list[str] = []

    async def has(self, sha256: str) -> bool:
        return sha256 in self._entries

    async def put(self, data: bytes) -> str:  # pragma: no cover - unused here
        raise NotImplementedError

    async def stats(self) -> PoolStats:  # pragma: no cover - unused here
        raise NotImplementedError

    async def list_entries(self) -> list[PoolEntry]:
        return list(self._entries.values())

    async def delete(self, sha256: str) -> None:
        self.deleted.append(sha256)
        self._entries.pop(sha256, None)


class _FakeReferences(LiveJarReferences):
    def __init__(self, keys: set[str]) -> None:
        self._keys = keys

    async def live(self) -> set[str]:
        return self._keys


def _old() -> dt.datetime:
    # Comfortably older than the safety window: never spared by it.
    return _NOW - GC_SAFETY_WINDOW - dt.timedelta(hours=1)


def _entry(sha: str, *, size: int = 10, age: dt.datetime | None = None) -> PoolEntry:
    return PoolEntry(sha256=sha, size_bytes=size, modified_at=age or _old())


def _gc(pool: _FakePool, refs: _FakeReferences) -> RunJarPoolGc:
    return RunJarPoolGc(pool=pool, references=refs, clock=_FixedClock(_NOW))


async def test_deletes_unreferenced_old_jar() -> None:
    pool = _FakePool([_entry("a" * 64, size=100)])
    refs = _FakeReferences(set())
    result = await _gc(pool, refs)()
    assert pool.deleted == ["a" * 64]
    assert result.scanned == 1
    assert result.deleted == 1
    assert result.freed_bytes == 100


async def test_keeps_referenced_jar() -> None:
    pool = _FakePool([_entry("a" * 64, size=100)])
    refs = _FakeReferences({"a" * 64})
    result = await _gc(pool, refs)()
    assert pool.deleted == []
    assert result.scanned == 1
    assert result.deleted == 0
    assert result.freed_bytes == 0


async def test_keeps_live_deletes_orphan_in_mixed_pool() -> None:
    live = _entry("a" * 64, size=10)
    orphan = _entry("b" * 64, size=20)
    pool = _FakePool([live, orphan])
    refs = _FakeReferences({"a" * 64})
    result = await _gc(pool, refs)()
    assert pool.deleted == ["b" * 64]
    assert result.scanned == 2
    assert result.deleted == 1
    assert result.freed_bytes == 20


async def test_safety_window_spares_a_too_young_orphan() -> None:
    # Younger than the window: an in-flight start may have put it before its row
    # committed (ensure_jar puts the JAR before StartServer commits the config).
    young = _entry(
        "c" * 64, size=30, age=_NOW - GC_SAFETY_WINDOW + dt.timedelta(minutes=1)
    )
    pool = _FakePool([young])
    refs = _FakeReferences(set())
    result = await _gc(pool, refs)()
    assert pool.deleted == []
    assert result.scanned == 1
    assert result.deleted == 0
    assert result.freed_bytes == 0


async def test_safety_window_boundary_is_inclusive_delete() -> None:
    # Exactly at the window edge counts as old enough to delete (>= window).
    at_edge = _entry("d" * 64, size=40, age=_NOW - GC_SAFETY_WINDOW)
    pool = _FakePool([at_edge])
    refs = _FakeReferences(set())
    result = await _gc(pool, refs)()
    assert pool.deleted == ["d" * 64]
    assert result.deleted == 1
    assert result.freed_bytes == 40
