"""Contract tests for :class:`ObjectResourcePackStore` (issue #2320).

The load-bearing assertion is ``size() == len(open())``: since #2317 the resource
pack download routes declare ``Content-Length`` from ``store.size()`` (a
``head_object`` ``ContentLength``) while the body streams from ``store.open()``
(a ``get_object`` body). A disagreement between the two hangs or corrupts the
response over HTTP/2 — on the public route every joining Minecraft client hits.

One set of assertions runs against the SAME adapter over two S3 backends, in the
spirit of the storage Port-contract harness (``tests/storage/test_port_contract``):

- ``fake-s3`` — the in-memory stub (``tests/storage/fake_s3``), always run.
- ``live-s3`` — a real S3-compatible endpoint, gated on ``MCD_TEST_S3_ENDPOINT``
  exactly like ``tests/storage/test_object_live_seaweedfs``; skipped cleanly when
  unset so ``make check`` / CI stay green without an S3 instance. This is the
  parametrization that matters for the byte-count invariant: only a real backend
  proves that its ``head`` ``ContentLength`` agrees with its ``get`` body.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.servers.adapters.resource_pack_store import (
    ObjectResourcePackStore,
)
from mc_server_dashboard_api.servers.domain.resource_pack import ResourcePackId
from mc_server_dashboard_api.storage.adapters.object_client import (
    make_s3_client_factory,
)
from mc_server_dashboard_api.storage.domain.errors import NotFoundError
from tests.storage.fake_s3 import (
    FakeS3Store,
    close_tracking_factory,
    fake_s3_factory,
)

_ENDPOINT = os.environ.get("MCD_TEST_S3_ENDPOINT")
_BUCKET = os.environ.get("MCD_TEST_S3_BUCKET", "mcsd")
_ACCESS_KEY = os.environ.get("MCD_TEST_S3_ACCESS_KEY", "mcsdaccess")
_SECRET_KEY = os.environ.get("MCD_TEST_S3_SECRET_KEY", "mcsdsecret")

_FILENAME = "pack.zip"
# Streamed in several chunks so ``put`` never sees the blob as one whole read.
_CHUNKS = [b"PK\x03\x04", b"x" * 1024, b"y" * 512]
_BLOB = b"".join(_CHUNKS)


@pytest.fixture(params=["fake-s3", "live-s3"])
def store(request: pytest.FixtureRequest) -> ObjectResourcePackStore:
    """The adapter over one S3 backend (in-memory stub / live endpoint)."""

    if request.param == "live-s3":
        if _ENDPOINT is None:
            pytest.skip("MCD_TEST_S3_ENDPOINT not set (no live S3 endpoint)")
        return ObjectResourcePackStore(
            make_s3_client_factory(
                endpoint=_ENDPOINT,
                bucket=_BUCKET,
                access_key=_ACCESS_KEY,
                secret_key=_SECRET_KEY,
                connect_timeout=10.0,
                read_timeout=60.0,
                retry_max_attempts=5,
            )
        )
    # close_tracking_factory guards every adapter test against the use-after-close
    # client leak (issue #952), as the storage harness does.
    return ObjectResourcePackStore(
        close_tracking_factory(fake_s3_factory(FakeS3Store()))
    )


@pytest.fixture
async def pack(store: ObjectResourcePackStore) -> AsyncIterator[ResourcePackId]:
    """A fresh pack id per test, removed afterwards so live runs leave no blobs."""

    pack_id = ResourcePackId(uuid.uuid4())
    yield pack_id
    await store.delete(pack_id)


async def _put(store: ObjectResourcePackStore, pack_id: ResourcePackId) -> None:
    async def _stream() -> AsyncIterator[bytes]:
        for chunk in _CHUNKS:
            yield chunk

    await store.put(pack_id, _FILENAME, _stream())


async def _read(store: ObjectResourcePackStore, pack_id: ResourcePackId) -> bytes:
    return b"".join([chunk async for chunk in store.open(pack_id, _FILENAME)])


async def test_put_then_open_round_trips_the_blob(
    store: ObjectResourcePackStore, pack: ResourcePackId
) -> None:
    await _put(store, pack)
    assert await _read(store, pack) == _BLOB


async def test_size_reports_the_open_byte_count(
    store: ObjectResourcePackStore, pack: ResourcePackId
) -> None:
    # The #2317 invariant: the declared Content-Length equals the streamed body.
    await _put(store, pack)
    assert await store.size(pack, _FILENAME) == len(await _read(store, pack))


async def test_size_of_unknown_pack_is_not_found_like_open(
    store: ObjectResourcePackStore, pack: ResourcePackId
) -> None:
    # Nothing was put: size() must fail the same way open() does, so the route
    # cannot declare a length for a body that will never stream.
    with pytest.raises(NotFoundError):
        await _read(store, pack)
    with pytest.raises(NotFoundError):
        await store.size(pack, _FILENAME)


async def test_size_of_unknown_filename_is_not_found_like_open(
    store: ObjectResourcePackStore, pack: ResourcePackId
) -> None:
    await _put(store, pack)
    with pytest.raises(NotFoundError):
        assert [chunk async for chunk in store.open(pack, "other.zip")]
    with pytest.raises(NotFoundError):
        await store.size(pack, "other.zip")


async def test_delete_removes_the_stored_blob(
    store: ObjectResourcePackStore, pack: ResourcePackId
) -> None:
    await _put(store, pack)
    await store.delete(pack)
    with pytest.raises(NotFoundError):
        await store.size(pack, _FILENAME)
