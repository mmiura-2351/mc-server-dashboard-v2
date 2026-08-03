"""Fidelity of :class:`FakeS3Client`'s store times against a real S3 backend (#2529).

This double stands in for the ``S3Client`` surface under ``tests/storage/`` (the
object-adapter tests). It is the sibling of the two Port-level fidelity suites
(``tests/versions/test_jar_pool_fake.py``,
``tests/servers/test_plugin_cache_store_fake.py``), and mirrors their shape so all
three fakes are guarded the same way. The difference the mirror exposes: those
doubles have one write path (``put``); this one has three (``put_object``,
``upload_multipart``, ``copy_object``), each stamping the store time the real
backend records as ``LastModified``. Before #2606 none of the three was pinned --
mutating any of them to the unstamped sentinel passed the whole suite (585 passed,
15 skipped). That asymmetry is exactly what PR #2603 recorded in prose in
``fake_s3.py`` *because* there was no test to record it in.

Pinned here:

- Each of the three write paths records a store time, which ``list_objects`` then
  reports as ``last_modified`` -- the real backend stamps every PutObject /
  CompleteMultipartUpload / CopyObject. Each is bracketed between two host-clock
  reads, so mutating one path's stamp reddens exactly that path's test.
- An object seeded straight into ``objects`` -- the GC tests' idiom -- reports a
  store time no clock can age past (the ``_UNSTAMPED_STORE_TIME`` sentinel), so
  forgetting the stamp reddens a delete-expecting test instead of passing by
  accident. That fail-loud property used to be a wall-clock read, i.e. an unstated
  environmental assumption underneath a safety property (#2529).

Unlike the two siblings, no re-put/dedup case is pinned: those doubles model a
Port whose adapter head-checks the content key and skips the upload, so a re-put
keeps the first stamp. This fake models the raw S3 client one level *below* that
dedup, where every PutObject/CopyObject re-stamps ``LastModified`` unconditionally;
the dedup skip lives in the adapter above and is exercised against it there. That
is the one intended difference from the sibling suites.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

from tests.storage.fake_s3 import FakeS3Client, FakeS3Store

_BODY = b"object-bytes"


async def _parts(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def test_put_object_records_the_store_time() -> None:
    store = FakeS3Store()
    client = FakeS3Client(store)
    before = dt.datetime.now(dt.UTC)

    await client.put_object("obj/a", _BODY)

    after = dt.datetime.now(dt.UTC)
    (obj,) = await client.list_objects("obj/")
    assert obj.key == "obj/a"
    assert before <= obj.last_modified <= after


async def test_upload_multipart_records_the_store_time() -> None:
    store = FakeS3Store()
    client = FakeS3Client(store)
    before = dt.datetime.now(dt.UTC)

    await client.upload_multipart("obj/a", _parts(_BODY))

    after = dt.datetime.now(dt.UTC)
    (obj,) = await client.list_objects("obj/")
    assert obj.key == "obj/a"
    assert before <= obj.last_modified <= after


async def test_copy_object_records_the_store_time() -> None:
    store = FakeS3Store()
    client = FakeS3Client(store)
    # Seed the source straight into the store so only the copy destination carries
    # a write stamp -- the source's absence of one is irrelevant to what the copy
    # records at ``dst``.
    store.objects["src/a"] = _BODY
    before = dt.datetime.now(dt.UTC)

    await client.copy_object("src/a", "dst/a")

    after = dt.datetime.now(dt.UTC)
    (obj,) = await client.list_objects("dst/")
    assert obj.key == "dst/a"
    assert before <= obj.last_modified <= after


async def test_unstamped_object_reports_a_store_time_no_clock_can_age() -> None:
    """An object seeded without a stamp is spared by the GC under *any* clock.

    Reading the host clock made that hold only while the clock ran later than the
    test's fixed ``now`` minus the safety window -- true in practice, but an
    environmental assumption propping up a fail-loud property (#2529). The store
    time is a fixed sentinel: stable across calls (so it is not the moment
    ``list_objects`` ran) and beyond any clock a test would use.
    """
    store = FakeS3Store()
    client = FakeS3Client(store)
    store.objects["obj/a"] = _BODY

    (first,) = await client.list_objects("obj/")
    (second,) = await client.list_objects("obj/")

    assert first.last_modified == second.last_modified
    assert first.last_modified > dt.datetime(3000, 1, 1, tzinfo=dt.UTC)
