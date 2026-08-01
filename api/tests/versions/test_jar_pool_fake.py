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
- ``delete`` absorbs a key that is not there, as both backends do.
- A re-put keeps the first stamp. This one is a *convention of the double*, not
  a backend contract -- see below.
- A JAR seeded straight into ``stored`` -- the GC tests' idiom -- reports a
  store time no clock can age past, so forgetting the stamp reddens a
  delete-expecting test instead of passing by accident. That fail-loud property
  used to be a wall-clock read, i.e. an unstated environmental assumption
  underneath a safety property (#2529).

The re-put case is the one place this double cannot mirror the adapters,
because they disagree: ``FsStorage.put_jar`` always ``os.replace``s the staged
file, refreshing ``st_mtime``, while ``ObjectStorage.put_jar`` head-checks the
content key and skips the upload, leaving ``last_modified`` alone. Neither
``JarPool.put`` nor ``JarStore.put_jar`` picks a side, so nothing here asserts
what the *backends* do. What is asserted is that the fake picks one and holds
it: a test that seeds a store time and then re-puts must keep reading the time
it seeded, or every ``put``-seeded GC test quietly changes meaning.
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


async def test_re_put_keeps_the_first_store_time() -> None:
    """The double keeps the first stamp; this is its convention, not a contract.

    The backends disagree on what a re-put does to the store time (see the
    module docstring), so nothing here claims either answer is *the* Port
    behaviour. What must hold is that the fake is deterministic about it: a GC
    test that seeds a store time and then re-puts the same bytes has to keep
    reading the time it seeded, otherwise the JAR silently becomes young and the
    test stops asserting what it says it asserts.
    """
    pool = FakeJarPool()
    key = await pool.put(_JAR)
    stored_at = dt.datetime(2026, 6, 5, 12, 0, tzinfo=dt.UTC)
    pool.modified_at[key] = stored_at

    await pool.put(_JAR)

    (entry,) = await pool.list_entries()
    assert entry.modified_at == stored_at


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


async def test_delete_of_absent_jar_is_idempotent() -> None:
    # Both backends absorb a missing key (``unlink(missing_ok=True)`` /
    # ``delete_object``), and the Port promises the same ("no error if absent").
    pool = FakeJarPool()

    await pool.delete("a" * 64)

    assert pool.deleted == ["a" * 64]


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
