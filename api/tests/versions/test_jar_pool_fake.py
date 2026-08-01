"""Fidelity of :class:`FakeJarPool` against the real adapters (#2529).

Since #2531 this double is the single description of the ``JarPool`` Port for
every test under ``tests/versions/``. It stands in for :class:`StorageJarPool`
over either storage backend, so it may only claim what *both* backends
guarantee. The sibling file for the plugin cache
(``tests/servers/test_plugin_cache_store_fake.py``) is the same shape.

Pinned here:

- ``put`` records a store time, which ``list_entries`` then reports. Both
  backends give a freshly pooled JAR one: the fs adapter renames the staged
  file into place (``st_mtime``), the object adapter uploads it
  (``last_modified``).
- A JAR seeded straight into ``stored`` -- the GC tests' idiom -- reports a
  store time no clock can age past, so forgetting the stamp reddens a
  delete-expecting test instead of passing by accident. That fail-loud property
  used to be a wall-clock read, i.e. an unstated environmental assumption
  underneath a safety property (#2529).

Deliberately *not* pinned: what a re-put of already-pooled bytes does to the
store time. The two backends disagree -- ``FsStorage.put_jar`` always
``os.replace``s the staged file, refreshing ``st_mtime``, while
``ObjectStorage.put_jar`` head-checks the content key and skips the upload,
leaving ``last_modified`` alone. Neither Port docstring picks a side, so a test
either way would pin one backend's behaviour as if it were the contract.
"""

from __future__ import annotations

import datetime as dt

from tests.versions.fakes import FakeJarPool

_JAR = b"jar-bytes"


async def test_put_records_the_store_time() -> None:
    pool = FakeJarPool()
    before = dt.datetime.now(dt.UTC)

    key = await pool.put(_JAR)

    after = dt.datetime.now(dt.UTC)
    (entry,) = await pool.list_entries()
    assert entry.sha256 == key
    assert before <= entry.modified_at <= after


async def test_put_after_delete_records_a_fresh_store_time() -> None:
    # Deleting drops the JAR, so the next put stores it again and it gets a new
    # store time -- it must not inherit the deleted JAR's.
    pool = FakeJarPool()
    key = await pool.put(_JAR)
    pool.modified_at[key] = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    await pool.delete(key)

    before = dt.datetime.now(dt.UTC)
    await pool.put(_JAR)

    (entry,) = await pool.list_entries()
    assert entry.modified_at >= before


async def test_unstamped_jar_reports_a_store_time_no_clock_can_age() -> None:
    """A JAR seeded without a stamp is spared by the GC under *any* clock.

    Reading the host clock made that hold only while the clock ran later than
    the test's fixed ``now`` minus the safety window -- true in practice, but an
    environmental assumption propping up a fail-loud property (#2529). The store
    time is now a fixed sentinel: stable across calls (so it is not the moment
    ``list_entries`` ran) and beyond any clock a test would use.
    """
    pool = FakeJarPool()
    pool.stored["a" * 64] = _JAR

    (first,) = await pool.list_entries()
    (second,) = await pool.list_entries()

    assert first.modified_at == second.modified_at
    assert first.modified_at > dt.datetime(3000, 1, 1, tzinfo=dt.UTC)
