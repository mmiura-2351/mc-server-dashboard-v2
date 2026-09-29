"""Fake-only S3 behavior outside the shared client contract."""

from __future__ import annotations

import datetime as dt

from tests.storage.fake_s3 import FakeS3Client, FakeS3Store

_BODY = b"object-bytes"
_UNSTAMPED_STORE_TIME = dt.datetime(9999, 1, 1, tzinfo=dt.UTC)


async def test_directly_seeded_object_reports_far_future_store_time() -> None:
    store = FakeS3Store()
    client = FakeS3Client(store)
    store.objects["obj/a"] = _BODY

    (obj,) = await client.list_objects("obj/")

    assert obj.last_modified == _UNSTAMPED_STORE_TIME
