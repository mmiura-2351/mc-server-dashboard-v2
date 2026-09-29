"""S3Client contract against the in-memory fake and an actual S3 endpoint."""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.storage.adapters.object_client import (
    make_s3_client_factory,
)
from mc_server_dashboard_api.storage.adapters.object_store import S3Client, S3Object
from mc_server_dashboard_api.storage.domain.errors import NotFoundError
from tests.storage.fake_s3 import FakeS3Client, FakeS3Store
from tests.store_clock import StoreClock

_ENDPOINT = os.environ.get("MCD_TEST_S3_ENDPOINT")


@pytest.fixture(params=["fake", "live-s3"])
async def s3(
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[S3Client, str, StoreClock, bool]]:
    clock = StoreClock()
    prefix = f"contract/{uuid.uuid4().hex}/"
    if request.param == "fake":
        yield FakeS3Client(FakeS3Store(clock=clock)), prefix, clock, True
        return
    if _ENDPOINT is None:
        pytest.skip("MCD_TEST_S3_ENDPOINT not set (no live S3 endpoint)")
    factory = make_s3_client_factory(
        endpoint=_ENDPOINT,
        bucket=os.environ.get("MCD_TEST_S3_BUCKET", "mcsd"),
        access_key=os.environ.get("MCD_TEST_S3_ACCESS_KEY", "mcsdaccess"),
        secret_key=os.environ.get("MCD_TEST_S3_SECRET_KEY", "mcsdsecret"),
        connect_timeout=10.0,
        read_timeout=60.0,
        retry_max_attempts=5,
    )
    async with factory() as client:
        try:
            yield client, prefix, clock, False
        finally:
            for obj in await client.list_objects(prefix):
                await client.delete_object(obj.key)


async def _read(client: S3Client, key: str) -> bytes:
    return b"".join([chunk async for chunk in await client.get_object(key)])


def _assert_store_time(obj: S3Object, clock: StoreClock, controlled: bool) -> None:
    assert obj.last_modified.tzinfo is not None
    assert obj.last_modified.utcoffset() == dt.timedelta(0)
    if controlled:
        assert obj.last_modified == clock.now
    else:
        # The live service owns its clock and may report only whole seconds. A
        # bounded value still catches an absent/sentinel LastModified mapping.
        assert dt.datetime(2000, 1, 1, tzinfo=dt.UTC) < obj.last_modified
        assert obj.last_modified < dt.datetime(3000, 1, 1, tzinfo=dt.UTC)


async def test_put_overwrites_one_object(
    s3: tuple[S3Client, str, StoreClock, bool],
) -> None:
    client, prefix, clock, controlled = s3
    key = prefix + "one"
    await client.put_object(key, b"old")
    clock.advance()

    await client.put_object(key, b"newer")

    assert await _read(client, key) == b"newer"
    (obj,) = await client.list_objects(prefix)
    assert (obj.key, obj.size) == (key, 5)
    _assert_store_time(obj, clock, controlled)


async def test_multipart_upload_is_listed_with_store_time(
    s3: tuple[S3Client, str, StoreClock, bool],
) -> None:
    client, prefix, clock, controlled = s3
    key = prefix + "multipart"

    async def parts() -> AsyncIterator[bytes]:
        yield b"part-one"
        yield b"part-two"

    await client.upload_multipart(key, parts())

    assert await _read(client, key) == b"part-onepart-two"
    (obj,) = await client.list_objects(prefix)
    assert (obj.key, obj.size) == (key, len(b"part-onepart-two"))
    _assert_store_time(obj, clock, controlled)


async def test_copy_creates_a_separate_object_with_store_time(
    s3: tuple[S3Client, str, StoreClock, bool],
) -> None:
    client, prefix, clock, controlled = s3
    source = prefix + "source"
    destination = prefix + "destination"
    await client.put_object(source, b"copy-me")
    clock.advance()

    await client.copy_object(source, destination)

    assert await _read(client, destination) == b"copy-me"
    (obj,) = await client.list_objects(destination)
    assert (obj.key, obj.size) == (destination, len(b"copy-me"))
    _assert_store_time(obj, clock, controlled)


async def test_missing_and_deleted_objects_are_absent(
    s3: tuple[S3Client, str, StoreClock, bool],
) -> None:
    client, prefix, _, _ = s3
    key = prefix + "one"
    assert await client.head_object(key) is None
    with pytest.raises(NotFoundError):
        await client.get_object(key)

    await client.delete_object(key)
    await client.put_object(key, b"contents")
    assert await client.head_object(key) == len(b"contents")
    await client.delete_object(key)
    await client.delete_object(key)

    assert await client.head_object(key) is None
    assert await client.list_objects(prefix) == []
