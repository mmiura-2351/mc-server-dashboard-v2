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
from contextlib import asynccontextmanager

import pytest

from mc_server_dashboard_api.servers.adapters.resource_pack_store import (
    ObjectResourcePackStore,
)
from mc_server_dashboard_api.servers.domain.errors import (
    ResourcePackNotFoundError,
    ResourcePackStorageUnavailableError,
)
from mc_server_dashboard_api.servers.domain.resource_pack import ResourcePackId
from mc_server_dashboard_api.storage.adapters.object_client import (
    make_s3_client_factory,
)
from mc_server_dashboard_api.storage.adapters.object_store import S3ClientFactory
from mc_server_dashboard_api.storage.domain.errors import ObjectStoreUnavailableError
from tests.storage.fake_s3 import (
    FakeS3Client,
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
    # cannot declare a length for a body that will never stream. The seam reports
    # the servers-layer error, which the routes map to 404 (issue #2321).
    with pytest.raises(ResourcePackNotFoundError):
        await _read(store, pack)
    with pytest.raises(ResourcePackNotFoundError):
        await store.size(pack, _FILENAME)


async def test_size_of_unknown_filename_is_not_found_like_open(
    store: ObjectResourcePackStore, pack: ResourcePackId
) -> None:
    await _put(store, pack)
    with pytest.raises(ResourcePackNotFoundError):
        assert [chunk async for chunk in store.open(pack, "other.zip")]
    with pytest.raises(ResourcePackNotFoundError):
        await store.size(pack, "other.zip")


async def test_delete_removes_the_stored_blob(
    store: ObjectResourcePackStore, pack: ResourcePackId
) -> None:
    await _put(store, pack)
    await store.delete(pack)
    with pytest.raises(ResourcePackNotFoundError):
        await store.size(pack, _FILENAME)


# --- the seam under a store outage (issue #2455) ---------------------------
#
# Only the in-memory backend: a live endpoint cannot be made to fail on demand,
# which is why these sit outside the ``store`` fixture's parametrization.


class _HeadUnavailableClient(FakeS3Client):
    """A client whose ``head_object`` fails the way a 5xx / transport fault does.

    The real client translates a backend 5xx or a transport failure on ``head``
    into ``ObjectStoreUnavailableError`` (issue #2376); the stub has no injection
    hook for that, so this raises it directly.
    """

    async def head_object(self, key: str) -> int | None:
        raise ObjectStoreUnavailableError(f"object store head failed for {key}")


def _head_unavailable_factory(store: FakeS3Store) -> S3ClientFactory:
    @asynccontextmanager
    async def _factory() -> AsyncIterator[_HeadUnavailableClient]:
        yield _HeadUnavailableClient(store)

    return _factory


async def test_open_backend_failure_translates_to_storage_unavailable() -> None:
    """The seam (issue #2455): the download routes begin the stream before they
    write the headers, so an outage on the locating half of the read still has a
    status to choose — it must arrive as the servers type the edge maps to 503,
    not as a raw storage type crossing back into the servers layer."""

    backing = FakeS3Store()
    store = ObjectResourcePackStore(close_tracking_factory(fake_s3_factory(backing)))
    pack_id = ResourcePackId(uuid.uuid4())
    await _put(store, pack_id)
    # Fail the body at offset 0: the store answers the GET with a fault before a
    # single byte, which is the shape an outage takes at the open.
    backing.read_aborts[f"resource-packs/{pack_id.value}/{_FILENAME}"] = [0]

    with pytest.raises(ResourcePackStorageUnavailableError):
        await _read(store, pack_id)


async def test_size_backend_failure_translates_to_storage_unavailable() -> None:
    """The probe sits in the same window as the open and answers the same route,
    so one outage must not yield 503 from one call and 500 from the other."""

    store = ObjectResourcePackStore(
        close_tracking_factory(_head_unavailable_factory(FakeS3Store()))
    )

    with pytest.raises(ResourcePackStorageUnavailableError):
        await store.size(ResourcePackId(uuid.uuid4()), _FILENAME)
