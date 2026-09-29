"""Fake-only JAR-pool behavior outside the shared Port contract."""

from __future__ import annotations

import datetime as dt
import hashlib

from tests.store_clock import StoreClock
from tests.versions.fakes import FakeJarPool

_JAR = b"jar-bytes"
_SHA = hashlib.sha256(_JAR).hexdigest()
_UNSTAMPED_STORE_TIME = dt.datetime(9999, 1, 1, tzinfo=dt.UTC)


async def test_directly_seeded_jar_reports_far_future_store_time() -> None:
    pool = FakeJarPool()
    pool.stored[_SHA] = _JAR

    (entry,) = await pool.list_entries()

    assert entry.modified_at == _UNSTAMPED_STORE_TIME


async def test_reput_preserves_first_store_time() -> None:
    clock = StoreClock()
    pool = FakeJarPool(clock=clock)
    key = await pool.put(_JAR)
    (first,) = await pool.list_entries()
    clock.advance()

    await pool.put(_JAR)

    (entry,) = await pool.list_entries()
    assert entry.sha256 == key
    assert entry.modified_at == first.modified_at
