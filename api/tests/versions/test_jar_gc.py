"""Reference-counted JAR-pool GC (D4, issue #293).

Unit tests over in-memory fakes for the JarPool (list/delete), the live
reference set, and a fixed clock — no storage, no DB, no network. Cover the
reference-set math (live kept, orphan deleted), the freed-bytes accounting, and
the safety window (a too-young orphan is spared even when unreferenced).

The pool double is the shared :class:`FakeJarPool` every other versions test
uses, so this file cannot drift away from the adapter's behaviour on its own
(issue #2528). A failing delete is driven through the real object-backed pool
over the faulty S3 stub instead, so it raises what production raises
(issue #3871).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import pytest

from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.errors import ObjectStoreUnreachableError
from mc_server_dashboard_api.versions.adapters.storage_jar_pool import StorageJarPool
from mc_server_dashboard_api.versions.application.jar_gc import (
    GC_SAFETY_WINDOW,
    RunJarPoolGc,
)
from mc_server_dashboard_api.versions.domain.clock import Clock
from mc_server_dashboard_api.versions.domain.errors import (
    JarPoolUnreachableError,
    StoreNotOwnedError,
)
from mc_server_dashboard_api.versions.domain.jar_pool import JarPool
from mc_server_dashboard_api.versions.domain.jar_references import LiveJarReferences
from tests.storage.fake_s3 import FakeS3Store
from tests.storage.faulty_s3 import Faults, faulty_s3_factory
from tests.store_ownership import FakeStoreOwnership
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
        # Optional: keys to add after the first live() call, simulating a server
        # row committing between the initial snapshot and a re-check.
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


def _gc(
    pool: JarPool,
    refs: _FakeReferences,
    ownership: FakeStoreOwnership | None = None,
) -> RunJarPoolGc:
    return RunJarPoolGc(
        pool=pool,
        references=refs,
        clock=_FixedClock(_NOW),
        ownership=ownership or FakeStoreOwnership(),
    )


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


async def test_recheck_before_delete_spares_newly_referenced_jar() -> None:
    """A JAR that becomes referenced between the initial snapshot and the delete
    is spared by the pre-delete re-check (issue #2541).

    On the object backend a re-put of an already-pooled JAR skips the upload, so
    ``ensure_jar`` re-pooling a JAR does not refresh its store time: the JAR stays
    old enough to delete and the safety window alone cannot protect it. It looks
    orphaned in the initial live() snapshot, but by the time the GC reaches the
    delete the start that needs it has committed its row. The GC re-checks live()
    immediately before each delete to catch this race.
    """
    sha = "e" * 64
    pool = FakeJarPool()
    _seed(pool, sha, size=50)
    refs = _FakeReferences(set())  # unreferenced at the initial snapshot
    # The server row commits after the first live() call (the initial snapshot)
    # but before the GC attempts the delete (the re-check).
    refs.add_after_first_call(sha)

    result = await _gc(pool, refs)()
    assert pool.deleted == []
    assert result.scanned == 1
    assert result.deleted == 0


async def test_refuses_a_pool_whose_store_another_database_owns() -> None:
    # Every JAR in a store shared with another database looks unreferenced to
    # this one; the pass must refuse before it deletes any (issue #3870).
    pool = FakeJarPool()
    _seed(pool, "a" * 64)
    ownership = FakeStoreOwnership.foreign()

    with pytest.raises(StoreNotOwnedError) as excinfo:
        await _gc(pool, _FakeReferences(set()), ownership)()

    assert ownership.refusal_reason is not None
    assert ownership.refusal_reason in str(excinfo.value)
    assert "a" * 64 in pool.stored


class _ObjectPool:
    """The real object-backed pool over a bucket whose calls a test can fail."""

    def __init__(self) -> None:
        self.faults = Faults()
        self.pool = StorageJarPool(
            ObjectStorage(faulty_s3_factory(FakeS3Store(clock=_old), self.faults))
        )

    async def seed(self, count: int) -> list[str]:
        """Pool *count* distinct old JARs; their keys, in the order a pass meets
        them."""

        return sorted([await self.pool.put(b"jar-%d" % i) for i in range(count)])

    def fail_deletes_of(self, sha: str) -> None:
        self.faults.when = lambda op, key: op == "delete_object" and sha in key

    async def keys(self) -> list[str]:
        return [entry.sha256 for entry in await self.pool.list_entries()]


async def test_a_failing_delete_skips_that_jar_and_the_pass_reclaims_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # One JAR whose delete keeps failing must not starve the ones after it
    # (issue #3871). It is the first the pass meets, so a pass that stopped at the
    # failure would reclaim nothing.
    store = _ObjectPool()
    failing, *others = await store.seed(3)
    store.fail_deletes_of(failing)

    with caplog.at_level(logging.INFO, logger="mc_server_dashboard_api"):
        result = await _gc(store.pool, _FakeReferences(set()))()

    assert await store.keys() == [failing]
    assert (result.scanned, result.deleted, result.failed) == (3, 2, 1)
    assert result.freed_bytes == len(b"jar-0") * 2
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert failing in warning.getMessage()
    assert "ObjectStoreUnavailableError: injected outage" in warning.getMessage()
    assert all(sha not in warning.getMessage() for sha in others)


async def test_logs_each_reclaimed_jar_and_the_pass_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # What a pass deleted must be traceable afterwards: the key, its size and its
    # age per JAR, then one summary line (issue #3871).
    pool = FakeJarPool()
    _seed(pool, "a" * 64, size=100)
    _seed(pool, "b" * 64, size=20)  # live

    with caplog.at_level(logging.DEBUG, logger="mc_server_dashboard_api"):
        await _gc(pool, _FakeReferences({"b" * 64}))()

    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    (reclaimed, summary) = infos
    assert "a" * 64 in reclaimed
    assert "100 bytes" in reclaimed
    assert "2:00:00" in reclaimed  # stored one hour past the one-hour window
    assert "b" * 64 not in " ".join(infos)
    for part in ("2 jar(s) listed", "1 reclaimed (100 bytes)", "0 failed", "in 0.0s"):
        assert part in summary


async def test_a_pass_that_reclaims_nothing_logs_its_summary_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A daily pass over a pool with no orphan is the common case; it must not
    # leave an INFO line every time.
    pool = FakeJarPool()
    _seed(pool, "a" * 64)

    with caplog.at_level(logging.DEBUG, logger="mc_server_dashboard_api"):
        await _gc(pool, _FakeReferences({"a" * 64}))()

    assert [r for r in caplog.records if r.levelno >= logging.INFO] == []
    assert any("0 reclaimed" in r.getMessage() for r in caplog.records)


async def test_a_pass_whose_every_delete_failed_logs_its_summary_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Nothing was reclaimed, but the pass is not empty: the summary is the one
    # line that counts the failures, so it stays visible.
    store = _ObjectPool()
    (failing,) = await store.seed(1)
    store.fail_deletes_of(failing)

    with caplog.at_level(logging.DEBUG, logger="mc_server_dashboard_api"):
        result = await _gc(store.pool, _FakeReferences(set()))()

    assert (result.deleted, result.failed) == (0, 1)
    (summary,) = [r for r in caplog.records if r.levelno == logging.INFO]
    assert "1 failed" in summary.getMessage()


async def test_a_cancelled_delete_ends_the_pass() -> None:
    # Shutdown cancels the loop's task mid-pass; the cancellation must reach the
    # loop, not be counted as one failed JAR (issue #3871).
    store = _ObjectPool()
    first, *_ = keys = await store.seed(2)
    store.fail_deletes_of(first)
    store.faults.error = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _gc(store.pool, _FakeReferences(set()))()

    assert await store.keys() == keys


async def test_a_store_that_gives_no_answer_ends_the_pass() -> None:
    # A delete the store never answered says nothing about this JAR: every later
    # delete would wait out the same timeouts. The pass ends instead of skipping
    # each JAR in turn (issue #3871).
    store = _ObjectPool()
    first, *_ = keys = await store.seed(2)
    store.fail_deletes_of(first)
    store.faults.error = ObjectStoreUnreachableError

    with pytest.raises(JarPoolUnreachableError):
        await _gc(store.pool, _FakeReferences(set()))()

    assert await store.keys() == keys
