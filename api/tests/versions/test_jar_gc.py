"""Reference-counted JAR-pool GC (D4, issue #293).

Unit tests over in-memory fakes for the JarPool (list/delete), the live
reference set, and a fixed clock — no storage, no DB, no network. Cover the
reference-set math (live kept, orphan deleted), the freed-bytes accounting, and
the safety window (a too-young orphan is spared even when unreferenced).

The pool double is the shared :class:`FakeJarPool` every other versions test
uses, so this file cannot drift away from the adapter's behaviour on its own
(issue #2528).
"""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.versions.application.jar_gc import (
    GC_SAFETY_WINDOW,
    RunJarPoolGc,
)
from mc_server_dashboard_api.versions.domain.clock import Clock
from mc_server_dashboard_api.versions.domain.jar_references import LiveJarReferences
from tests.versions.fakes import FakeJarPool

_NOW = dt.datetime(2026, 6, 5, 12, 0, 0, tzinfo=dt.UTC)


class _FixedClock(Clock):
    def __init__(self, now: dt.datetime) -> None:
        self._now = now

    def now(self) -> dt.datetime:
        return self._now


class _FakeReferences(LiveJarReferences):
    def __init__(self, keys: set[str]) -> None:
        self._keys = keys

    async def live(self) -> set[str]:
        return self._keys


def _old() -> dt.datetime:
    # Comfortably older than the safety window: never spared by it.
    return _NOW - GC_SAFETY_WINDOW - dt.timedelta(hours=1)


def _seed(
    pool: FakeJarPool,
    sha: str,
    *,
    size: int = 10,
    age: dt.datetime | None = None,
) -> None:
    """Pool a JAR of *size* bytes under *sha*, stored at *age* (default: old)."""
    pool.stored[sha] = b"x" * size
    pool.modified_at[sha] = age or _old()


def _gc(pool: FakeJarPool, refs: _FakeReferences) -> RunJarPoolGc:
    return RunJarPoolGc(pool=pool, references=refs, clock=_FixedClock(_NOW))


async def test_deletes_unreferenced_old_jar() -> None:
    pool = FakeJarPool()
    _seed(pool, "a" * 64, size=100)
    refs = _FakeReferences(set())
    result = await _gc(pool, refs)()
    assert pool.deleted == ["a" * 64]
    assert result.scanned == 1
    assert result.deleted == 1
    assert result.freed_bytes == 100


async def test_keeps_referenced_jar() -> None:
    pool = FakeJarPool()
    _seed(pool, "a" * 64, size=100)
    refs = _FakeReferences({"a" * 64})
    result = await _gc(pool, refs)()
    assert pool.deleted == []
    assert result.scanned == 1
    assert result.deleted == 0
    assert result.freed_bytes == 0


async def test_keeps_live_deletes_orphan_in_mixed_pool() -> None:
    pool = FakeJarPool()
    _seed(pool, "a" * 64, size=10)  # live
    _seed(pool, "b" * 64, size=20)  # orphan
    refs = _FakeReferences({"a" * 64})
    result = await _gc(pool, refs)()
    assert pool.deleted == ["b" * 64]
    assert result.scanned == 2
    assert result.deleted == 1
    assert result.freed_bytes == 20


async def test_safety_window_spares_a_too_young_orphan() -> None:
    # Younger than the window: an in-flight start may have put it before its row
    # committed (ensure_jar puts the JAR before StartServer commits the config).
    pool = FakeJarPool()
    _seed(
        pool,
        "c" * 64,
        size=30,
        age=_NOW - GC_SAFETY_WINDOW + dt.timedelta(minutes=1),
    )
    refs = _FakeReferences(set())
    result = await _gc(pool, refs)()
    assert pool.deleted == []
    assert result.scanned == 1
    assert result.deleted == 0
    assert result.freed_bytes == 0


async def test_safety_window_boundary_is_inclusive_delete() -> None:
    # Exactly at the window edge counts as old enough to delete (>= window).
    pool = FakeJarPool()
    _seed(pool, "d" * 64, size=40, age=_NOW - GC_SAFETY_WINDOW)
    refs = _FakeReferences(set())
    result = await _gc(pool, refs)()
    assert pool.deleted == ["d" * 64]
    assert result.deleted == 1
    assert result.freed_bytes == 40
