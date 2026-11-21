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
import errno
import hashlib
import logging
import os
import random
from pathlib import Path

import pytest

from mc_server_dashboard_api.gc_pass import ResumePoint
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
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
        self._fail_after_first: Exception | None = None
        self._calls = 0

    def add_after_first_call(self, key: str) -> None:
        """Schedule *key* to appear in live() only from the second call onward."""
        self._add_after_first.add(key)

    def fail_after_first_call(self, error: Exception) -> None:
        """Make every live() call after the first raise *error*."""
        self._fail_after_first = error

    async def live(self) -> set[str]:
        self._calls += 1
        if self._calls > 1:
            if self._fail_after_first is not None:
                raise self._fail_after_first
            self._keys = self._keys | self._add_after_first
        return self._keys


def _resume_point() -> ResumePoint:
    """A resume point whose pass without a mark starts at the first key in key
    order and walks up, so a pass meets the keys in the order a test seeds them
    (issues #3934, #3965)."""

    return ResumePoint(start=lambda count: 0, descending=lambda: False)


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
    resume_point: ResumePoint | None = None,
) -> RunJarPoolGc:
    return RunJarPoolGc(
        pool=pool,
        references=refs,
        clock=_FixedClock(_NOW),
        ownership=ownership or FakeStoreOwnership(),
        resume_point=resume_point or _resume_point(),
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


class _DatabaseDown(Exception):
    """The reference read failing: the database, not the pool's store."""


async def test_a_failed_re_check_ends_the_pass_and_is_not_a_failed_delete() -> None:
    # The re-check before each delete sits outside the per-JAR ``try``: a re-read
    # that fails says nothing about the JAR, and without it no JAR may be deleted.
    # It leaves the pass rather than being counted as a failed delete
    # (issue #3906).
    pool = FakeJarPool()
    _seed(pool, "a" * 64)
    _seed(pool, "b" * 64)
    refs = _FakeReferences(set())
    refs.fail_after_first_call(_DatabaseDown("re-read failed"))

    with pytest.raises(_DatabaseDown):
        await _gc(pool, refs)()

    assert pool.deleted == []


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
        # The store time each put records; a test moves it to pool a young JAR.
        self.stamp = _old()
        self.pool = StorageJarPool(
            ObjectStorage(
                faulty_s3_factory(FakeS3Store(clock=lambda: self.stamp), self.faults)
            )
        )

    async def seed(self, count: int, *, young: int | None = None) -> list[str]:
        """Pool *count* distinct JARs; their keys, in the order a pass meets them.
        All are old, except the one at index *young* of that order, which is
        pooled now."""

        contents = sorted(
            (b"jar-%d" % i for i in range(count)),
            key=lambda d: hashlib.sha256(d).hexdigest(),
        )
        keys = []
        for index, data in enumerate(contents):
            self.stamp = _NOW if index == young else _old()
            keys.append(await self.pool.put(data))
        return keys

    def fail_deletes_of(self, *shas: str) -> None:
        self.faults.when = lambda op, key: (
            op == "delete_object" and any(sha in key for sha in shas)
        )

    def answer_no_delete(self) -> list[str]:
        """Give no answer to any delete; the JARs they were for, as they come."""

        attempted: list[str] = []

        def _fails(op: str, key: str) -> bool:
            if op != "delete_object":
                return False
            attempted.append(key.removeprefix("jars/").removesuffix(".jar"))
            return True

        self.faults.when = _fails
        self.faults.error = ObjectStoreUnreachableError
        return attempted

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


async def test_five_failed_deletes_in_a_row_end_the_pass(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A store that fails every delete costs each one its retries and a WARNING
    # with a traceback. Five in a row end the pass, which still logs its summary,
    # at WARNING with the reason; the JAR after them is not tried (issue #3906).
    store = _ObjectPool()
    keys = await store.seed(6)
    store.fail_deletes_of(*keys[:5])

    with caplog.at_level(logging.DEBUG, logger="mc_server_dashboard_api"):
        result = await _gc(store.pool, _FakeReferences(set()))()

    assert await store.keys() == keys
    assert (result.scanned, result.deleted, result.failed) == (6, 0, 5)
    summary = caplog.records[-1]
    assert summary.levelno == logging.WARNING
    assert vars(summary).get("reason") == "stopped_after_consecutive_failures"
    assert "6 jar(s) listed, 0 reclaimed (0 bytes), 5 failed" in summary.getMessage()


async def test_a_reclaimed_jar_restarts_the_count_of_failed_deletes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Four failures, one success, four more: never five in a row, so the pass
    # reaches every JAR and ends as usual (issue #3906).
    store = _ObjectPool()
    keys = await store.seed(9)
    reclaimed = keys.pop(4)
    store.fail_deletes_of(*keys)

    with caplog.at_level(logging.DEBUG, logger="mc_server_dashboard_api"):
        result = await _gc(store.pool, _FakeReferences(set()))()

    assert reclaimed not in await store.keys()
    assert await store.keys() == keys
    assert (result.scanned, result.deleted, result.failed) == (9, 1, 8)
    summary = caplog.records[-1]
    assert summary.levelno == logging.INFO
    assert "reason" not in vars(summary)


async def test_a_skipped_jar_leaves_the_count_of_failed_deletes_alone() -> None:
    # A real pool mixes live and young JARs among the orphans. A JAR the pass
    # skips says nothing about the store, so it neither adds to the count nor
    # restarts it: five failures with a live, a young and a re-check-spared JAR
    # between them still end the pass, and the JAR after them is not tried
    # (issue #3906).
    store = _ObjectPool()
    keys = await store.seed(9, young=4)
    live, young, spared, last = keys[2], keys[4], keys[6], keys[8]
    store.fail_deletes_of(*(k for k in keys if k not in (live, young, spared, last)))
    refs = _FakeReferences({live})
    refs.add_after_first_call(spared)

    result = await _gc(store.pool, refs)()

    assert await store.keys() == keys
    assert (result.scanned, result.deleted, result.failed) == (9, 0, 5)


async def test_a_jar_the_store_gives_no_answer_for_starves_no_other() -> None:
    # A delete that gets no answer ends the pass (issue #3871). When that failure
    # is in fact tied to one JAR, the next pass starts after it and comes round to
    # it last, so every other orphan is reclaimed within two passes, and the JAR
    # is still tried (issue #3934).
    store = _ObjectPool()
    keys = await store.seed(5)
    bad = keys[1]
    store.fail_deletes_of(bad)
    store.faults.error = ObjectStoreUnreachableError
    gc = _gc(store.pool, _FakeReferences(set()), resume_point=_resume_point())

    with pytest.raises(JarPoolUnreachableError):
        await gc()
    with pytest.raises(JarPoolUnreachableError):
        await gc()

    assert await store.keys() == [bad]


async def test_a_store_that_answers_no_delete_ends_each_pass_at_its_first() -> None:
    # A store that is truly down still ends each pass at once. Each pass starts
    # after the JAR the last one ended at, so the passes take the JARs in turn,
    # and the resume point is only ever that one JAR (issue #3934).
    store = _ObjectPool()
    keys = await store.seed(3)
    attempted = store.answer_no_delete()
    resume_point = _resume_point()
    gc = _gc(store.pool, _FakeReferences(set()), resume_point=resume_point)

    for _ in range(7):
        with pytest.raises(JarPoolUnreachableError):
            await gc()

    assert attempted == [*keys, *keys, keys[0]]
    assert resume_point.key == keys[0]
    assert await store.keys() == keys


async def test_a_pass_that_comes_round_clears_the_resume_point() -> None:
    # A pass that walks the whole listing without a store failure leaves no resume
    # point, so the pass after it starts at the beginning (issue #3934).
    store = _ObjectPool()
    keys = await store.seed(3)
    store.fail_deletes_of(keys[1])
    store.faults.error = ObjectStoreUnreachableError
    resume_point = _resume_point()
    gc = _gc(store.pool, _FakeReferences(set()), resume_point=resume_point)
    with pytest.raises(JarPoolUnreachableError):
        await gc()
    assert resume_point.key == keys[1]
    store.faults.clear()

    await gc()

    assert resume_point.key is None


async def test_a_resumed_pass_logs_where_it_starts_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Where a pass starts is worth a line only when debugging (issue #3934).
    store = _ObjectPool()
    keys = await store.seed(3)
    store.fail_deletes_of(keys[1])
    store.faults.error = ObjectStoreUnreachableError
    gc = _gc(store.pool, _FakeReferences(set()), resume_point=_resume_point())
    with pytest.raises(JarPoolUnreachableError):
        await gc()
    store.faults.clear()

    with caplog.at_level(logging.DEBUG, logger="mc_server_dashboard_api"):
        await gc()

    (resumed,) = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and keys[1] in r.getMessage()
    ]
    assert "after" in resumed.getMessage()


async def test_a_jar_whose_unlink_fails_with_eio_does_not_starve_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fs reads EIO from unlink as a gone store, since a soft NFS mount reports a
    # dead server that way (issue #3906). One JAR on a bad local disk that answers
    # EIO therefore ends each pass at that JAR; the next pass starts after it
    # (issue #3934). It is the lowest key, so on any filesystem the first pass
    # ends at it before reclaiming anything, and only a pass that starts after it
    # reaches the others.
    pool = StorageJarPool(FsStorage(tmp_path))
    keys = [await pool.put(b"jar-%d" % index) for index in range(5)]
    for path in (tmp_path / "jars").iterdir():
        os.utime(path, (_old().timestamp(), _old().timestamp()))
    bad = min(keys)
    unlink = Path.unlink

    def _unlink(self: Path, missing_ok: bool = False) -> None:
        if self.stem == bad:
            raise OSError(errno.EIO, os.strerror(errno.EIO), str(self))
        unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _unlink)
    gc = _gc(pool, _FakeReferences(set()), resume_point=_resume_point())

    for _ in range(2):
        with pytest.raises(JarPoolUnreachableError):
            await gc()

    assert [entry.sha256 for entry in await pool.list_entries()] == [bad]


async def test_a_pass_five_failures_in_a_row_end_resumes_after_the_fifth() -> None:
    # A pass that stops after five failed deletes in a row ends early too, so the
    # next pass starts after the JAR whose failure ended it, wherever the last
    # pass started (issue #3934).
    store = _ObjectPool()
    keys = await store.seed(6)
    store.fail_deletes_of(keys[0])
    store.faults.error = ObjectStoreUnreachableError
    resume_point = _resume_point()
    gc = _gc(store.pool, _FakeReferences(set()), resume_point=resume_point)
    with pytest.raises(JarPoolUnreachableError):
        await gc()
    store.faults.clear()
    store.fail_deletes_of(*keys[1:])

    result = await gc()

    assert result.failed == 5
    assert resume_point.key == keys[5]


async def test_ten_standing_failures_in_a_row_starve_no_jar_after_them() -> None:
    # Ten JARs whose deletes keep failing, next to each other in key order: each
    # pass stops after five of them. Each starts after the last, so the third pass
    # reaches the JAR above them (issue #3934).
    store = _ObjectPool()
    keys = await store.seed(11)
    store.fail_deletes_of(*keys[:10])
    gc = _gc(store.pool, _FakeReferences(set()), resume_point=_resume_point())

    results = [await gc() for _ in range(3)]

    assert [result.failed for result in results] == [5, 5, 5]
    assert await store.keys() == keys[:10]


async def test_a_gc_restarted_before_every_pass_still_reclaims_past_a_bad_jar() -> None:
    # A process that ends before its next pass (one that lives under a day at the
    # default interval) never resumes after the JAR its pass ended at. Its first
    # pass starts at a random place in key order instead, so across restarts the
    # JARs after that one are still reached (issue #3934).
    store = _ObjectPool()
    keys = await store.seed(5)
    bad = keys[1]
    store.fail_deletes_of(bad)
    store.faults.error = ObjectStoreUnreachableError
    rng = random.Random(3934)

    for _ in range(10):
        restarted = _gc(
            store.pool,
            _FakeReferences(set()),
            resume_point=ResumePoint(start=rng.randrange),
        )
        with pytest.raises(JarPoolUnreachableError):
            await restarted()

    assert await store.keys() == [bad]
