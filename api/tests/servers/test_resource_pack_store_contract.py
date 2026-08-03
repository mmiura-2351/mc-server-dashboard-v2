"""Backend-agnostic ``ResourcePackStore`` contract, parametrized over the real
adapter and the in-memory fake (issue #2351).

The double :class:`FakeResourcePackStore` stands in for
:class:`ObjectResourcePackStore` in every use-case and route test, so the two
must answer identically or a route passes against the fake and fails in
production. Asserting that by hand in two files let them drift — precisely what
the last four PRs repaired (#2330/#2334 KeyError-vs-``NotFoundError`` and eager-
vs-lazy; #2335 the ignored ``filename``; #2321 the seam's exception type). This
runs every contract assertion against BOTH implementations at once, in the shape
``tests/storage/test_port_contract.py`` uses, so a divergence reddens one arm
here rather than surviving as a hidden mismatch.

The ``store`` fixture parametrizes over three implementations:

- ``fake`` — :class:`FakeResourcePackStore`, the use-case double.
- ``fake-s3`` — the real adapter over the in-memory S3 stub
  (``tests/storage/fake_s3``), always run.
- ``live-s3`` — the real adapter over a real S3-compatible endpoint, gated on
  ``MCD_TEST_S3_ENDPOINT`` exactly like ``tests/storage/test_object_live_seaweedfs``;
  skipped cleanly when unset so ``make check`` / CI stay green without an S3
  instance. This is the arm that matters for the byte-count invariant (#2317):
  only a real backend proves its ``head`` ``ContentLength`` agrees with its
  ``get`` body.

Store-outage translation (issue #2455) is adapter-only — a live endpoint cannot
be made to fail on demand and the fake has no fault-injection hook — so those
assertions sit outside the parametrization, against the adapter directly.
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
from mc_server_dashboard_api.servers.domain.resource_pack_store import (
    ResourcePackStore,
)
from mc_server_dashboard_api.storage.adapters.object_client import (
    make_s3_client_factory,
)
from mc_server_dashboard_api.storage.adapters.object_store import S3ClientFactory
from mc_server_dashboard_api.storage.domain.errors import ObjectStoreUnavailableError
from tests.servers.fakes import FakeResourcePackStore
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


@pytest.fixture(params=["fake", "fake-s3", "live-s3"])
def store(request: pytest.FixtureRequest) -> ResourcePackStore:
    """One ``ResourcePackStore`` implementation: the fake or the adapter."""

    if request.param == "fake":
        return FakeResourcePackStore()
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
async def pack(store: ResourcePackStore) -> AsyncIterator[ResourcePackId]:
    """A fresh pack id per test, removed afterwards so live runs leave no blobs."""

    pack_id = ResourcePackId(uuid.uuid4())
    yield pack_id
    await store.delete(pack_id)


async def _put(
    store: ResourcePackStore, pack_id: ResourcePackId, filename: str = _FILENAME
) -> None:
    async def _stream() -> AsyncIterator[bytes]:
        for chunk in _CHUNKS:
            yield chunk

    await store.put(pack_id, filename, _stream())


async def _read(
    store: ResourcePackStore, pack_id: ResourcePackId, filename: str = _FILENAME
) -> bytes:
    return b"".join([chunk async for chunk in store.open(pack_id, filename)])


async def test_put_then_open_round_trips_the_blob(
    store: ResourcePackStore, pack: ResourcePackId
) -> None:
    await _put(store, pack)
    assert await _read(store, pack) == _BLOB


async def test_size_reports_the_open_byte_count(
    store: ResourcePackStore, pack: ResourcePackId
) -> None:
    # The #2317 invariant: the declared Content-Length equals the streamed body.
    await _put(store, pack)
    assert await store.size(pack, _FILENAME) == len(await _read(store, pack))


async def test_open_of_unknown_pack_surfaces_not_found_lazily(
    store: ResourcePackStore, pack: ResourcePackId
) -> None:
    # ``open`` does no I/O itself: it hands back a generator and the miss surfaces
    # on the first chunk, not on the call (issue #2334 — the fake once raised
    # eagerly where the adapter raises lazily). So ``open`` must NOT raise, and the
    # iteration must raise the servers-layer ``ResourcePackNotFoundError`` (never a
    # ``KeyError`` or a raw storage type, #2321).
    stream = store.open(pack, _FILENAME)
    with pytest.raises(ResourcePackNotFoundError):
        assert [chunk async for chunk in stream]


async def test_size_of_unknown_pack_is_not_found(
    store: ResourcePackStore, pack: ResourcePackId
) -> None:
    # Nothing was put: size() must fail the same way open() does, so the route
    # cannot declare a length for a body that will never stream. The seam reports
    # the servers-layer error, which the routes map to 404 (issue #2321).
    with pytest.raises(ResourcePackNotFoundError):
        await store.size(pack, _FILENAME)


async def test_size_and_open_of_unknown_filename_are_not_found(
    store: ResourcePackStore, pack: ResourcePackId
) -> None:
    # The adapter keys on ``resource-packs/<pack-id>/<filename>``, so a stored pack
    # read under a different filename misses (issue #2335). A fake keyed on the pack
    # id alone would serve the blob and hide a production 404.
    await _put(store, pack)
    with pytest.raises(ResourcePackNotFoundError):
        assert [chunk async for chunk in store.open(pack, "other.zip")]
    with pytest.raises(ResourcePackNotFoundError):
        await store.size(pack, "other.zip")


async def test_delete_removes_the_stored_blob(
    store: ResourcePackStore, pack: ResourcePackId
) -> None:
    await _put(store, pack)
    await store.delete(pack)
    with pytest.raises(ResourcePackNotFoundError):
        await store.size(pack, _FILENAME)


async def test_delete_sweeps_every_filename_under_the_pack_id(
    store: ResourcePackStore, pack: ResourcePackId
) -> None:
    # delete() takes a pack id, not a filename: the adapter drops the whole
    # ``resource-packs/<pack-id>/`` prefix, so every blob under that pack goes at
    # once. Pin that both implementations sweep across multiple filenames — the
    # fake's per-filename keying must not leave a sibling behind (issue #2351).
    await _put(store, pack, "one.zip")
    await _put(store, pack, "two.zip")

    await store.delete(pack)

    for filename in ("one.zip", "two.zip"):
        with pytest.raises(ResourcePackNotFoundError):
            await store.size(pack, filename)


# --- the seam under a store outage (issue #2455) ---------------------------
#
# Adapter-only: a live endpoint cannot be made to fail on demand and the fake has
# no fault-injection hook, which is why these sit outside the ``store``
# parametrization and drive the adapter directly.


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
