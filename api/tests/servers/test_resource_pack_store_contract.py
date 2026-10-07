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

Store-outage translation (issues #2455, #2458) is adapter-only — a live endpoint cannot
be made to fail on demand and the fake has no fault-injection hook — so those
assertions sit outside the parametrization, against the adapter directly.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import uuid
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from mc_server_dashboard_api.servers.adapters.resource_pack_store import (
    ObjectResourcePackStore,
)
from mc_server_dashboard_api.servers.application.resource_packs import (
    UploadResourcePack,
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
    _Aioboto3S3Client,
    make_s3_client_factory,
)
from mc_server_dashboard_api.storage.adapters.object_store import (
    S3ClientFactory,
    S3Object,
)
from mc_server_dashboard_api.storage.domain.errors import ObjectStoreUnavailableError
from tests.servers.fakes import FakeClock, FakeResourcePackStore, FakeUnitOfWork
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


async def test_delete_of_an_already_deleted_pack_is_a_no_op(
    store: ResourcePackStore, pack: ResourcePackId
) -> None:
    # Delete is idempotent (issue #2458): repeating one that already ran -- or
    # running one for a pack that never stored a blob -- finds nothing under the
    # prefix and succeeds, on a real backend as on the doubles. That is what makes
    # re-running a delete an outage interrupted safe.
    await _put(store, pack)
    await store.delete(pack)

    await store.delete(pack)

    with pytest.raises(ResourcePackNotFoundError):
        await store.size(pack, _FILENAME)


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


# --- the write paths under a store outage (issue #2458) --------------------


class _WriteFaultClient(FakeS3Client):
    """A client whose writes fail the way the real client reports an outage.

    The real client translates a backend fault or a transport failure on
    ``upload_multipart`` / ``list_objects`` / ``delete_object`` into
    ``ObjectStoreUnavailableError`` (issues #2270, #2273, #2378) -- after aborting
    the multipart upload, for an upload (pinned against the client itself in
    ``tests/storage/test_object_client.py``). The stub has no injection hook for
    that, so this raises the typed error directly.

    ``fail_delete_of`` fails the delete of exactly those keys, so a delete can be
    interrupted partway through a prefix.
    """

    def __init__(
        self,
        store: FakeS3Store,
        *,
        fail_upload: bool = False,
        fail_list: bool = False,
        fail_delete_of: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(store)
        self._fail_upload = fail_upload
        self._fail_list = fail_list
        self._fail_delete_of = fail_delete_of

    async def upload_multipart(self, key: str, parts: AsyncIterator[bytes]) -> None:
        if self._fail_upload:
            # Like the real client, part of the body is consumed before the fault
            # and nothing is published: an incomplete multipart upload is never an
            # object.
            async for _ in parts:
                break
            raise ObjectStoreUnavailableError(f"object store upload failed for {key}")
        await super().upload_multipart(key, parts)

    async def list_objects(self, prefix: str) -> list[S3Object]:
        if self._fail_list:
            raise ObjectStoreUnavailableError(f"object store list failed for {prefix}")
        return await super().list_objects(prefix)

    async def delete_object(self, key: str) -> None:
        if key in self._fail_delete_of:
            raise ObjectStoreUnavailableError(f"object store delete failed for {key}")
        await super().delete_object(key)


def _write_fault_factory(store: FakeS3Store, **faults: object) -> S3ClientFactory:
    @asynccontextmanager
    async def _factory() -> AsyncIterator[_WriteFaultClient]:
        yield _WriteFaultClient(store, **faults)  # type: ignore[arg-type]

    return _factory


async def test_put_backend_failure_translates_and_leaves_no_blob() -> None:
    """An upload interrupted before the store published it crosses the seam as
    the servers type the edge maps to 503, and leaves no blob. (A fault AFTER
    publication is the other aftermath -- see the lost-completion test below.)"""

    backing = FakeS3Store()
    store = ObjectResourcePackStore(
        close_tracking_factory(_write_fault_factory(backing, fail_upload=True))
    )
    pack_id = ResourcePackId(uuid.uuid4())

    with pytest.raises(ResourcePackStorageUnavailableError):
        await _put(store, pack_id)

    assert backing.objects == {}


async def test_delete_backend_failure_at_the_listing_translates() -> None:
    backing = FakeS3Store()
    healthy = ObjectResourcePackStore(close_tracking_factory(fake_s3_factory(backing)))
    pack_id = ResourcePackId(uuid.uuid4())
    await _put(healthy, pack_id)
    store = ObjectResourcePackStore(
        close_tracking_factory(_write_fault_factory(backing, fail_list=True))
    )

    with pytest.raises(ResourcePackStorageUnavailableError):
        await store.delete(pack_id)

    # Nothing was removed: the listing is the first thing a delete does.
    assert await healthy.size(pack_id, _FILENAME) == len(_BLOB)


async def test_delete_interrupted_partway_translates_and_a_retry_finishes_it() -> None:
    """A delete that removed some of a pack's objects before the store failed
    reports the outage as the servers type, and re-running it is safe: the retry
    lists only what is left, so it never trips over the objects already gone."""

    backing = FakeS3Store()
    healthy = ObjectResourcePackStore(close_tracking_factory(fake_s3_factory(backing)))
    pack_id = ResourcePackId(uuid.uuid4())
    await _put(healthy, pack_id, "one.zip")
    await _put(healthy, pack_id, "two.zip")
    prefix = f"resource-packs/{pack_id.value}/"
    store = ObjectResourcePackStore(
        close_tracking_factory(
            _write_fault_factory(
                backing, fail_delete_of=frozenset({f"{prefix}two.zip"})
            )
        )
    )

    with pytest.raises(ResourcePackStorageUnavailableError):
        await store.delete(pack_id)

    # Partial: the first object went, the one the fault struck is still there.
    assert list(backing.objects) == [f"{prefix}two.zip"]

    await healthy.delete(pack_id)

    assert backing.objects == {}


# --- an upload the store completed but could not acknowledge (issue #2458) --


class _LostCompletionS3:
    """A raw aioboto3-client double whose first ``CompleteMultipartUpload``
    succeeds at the store while its response is lost.

    The object is published and the call then raises the read timeout the caller
    sees. The cleanup abort gets ``NoSuchUpload``, as real S3 answers for an upload
    id that has already completed -- an abort cannot undo a completed object.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self._parts: dict[str, bytearray] = {}
        self._lose_next_completion = True
        self.abort_calls = 0

    async def create_multipart_upload(self, **kwargs: str) -> dict[str, object]:
        self._parts[kwargs["Key"]] = bytearray()
        return {"UploadId": "upload"}

    async def upload_part(self, **kwargs: object) -> dict[str, object]:
        body = kwargs["Body"]
        assert isinstance(body, bytes)
        self._parts[str(kwargs["Key"])].extend(body)
        return {"ETag": "etag"}

    async def complete_multipart_upload(self, **kwargs: object) -> None:
        key = str(kwargs["Key"])
        self.objects[key] = bytes(self._parts.pop(key))
        if self._lose_next_completion:
            self._lose_next_completion = False
            raise ReadTimeoutError(endpoint_url="http://store:8333")

    async def abort_multipart_upload(self, **_kwargs: object) -> None:
        self.abort_calls += 1
        raise ClientError({"Error": {"Code": "NoSuchUpload"}}, "AbortMultipartUpload")


def _valid_pack_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pack.mcmeta", json.dumps({"pack": {"pack_format": 15}}))
    return buf.getvalue()


async def test_upload_completed_but_unacknowledged_leaves_a_blob_and_no_row() -> None:
    """The aftermath a pre-publication fault does not show, driven through the
    real object client and the adapter: the store completed the upload, the
    response was lost, and the abort had nothing left to undo. The outage still
    crosses the seam as the servers type, the completed blob SURVIVES, and no row
    names it -- so it is unreferenced, never served, and left for issue #3277 to
    reclaim. The retry is independent of it: a fresh pack id, hence a fresh key."""

    raw = _LostCompletionS3()

    @asynccontextmanager
    async def _factory() -> AsyncIterator[_Aioboto3S3Client]:
        yield _Aioboto3S3Client(raw, "bucket")

    uow = FakeUnitOfWork()
    upload = UploadResourcePack(
        uow=uow,
        store=ObjectResourcePackStore(_factory),
        clock=FakeClock(dt.datetime(2026, 6, 16, tzinfo=dt.UTC)),
    )

    with pytest.raises(ResourcePackStorageUnavailableError):
        await upload(
            filename=_FILENAME,
            display_name="Pack",
            content=_valid_pack_zip(),
            uploaded_by=uuid.uuid4(),
        )

    assert raw.abort_calls == 1
    (orphan_key,) = raw.objects
    assert uow.resource_packs.packs == {}

    pack = await upload(
        filename=_FILENAME,
        display_name="Pack",
        content=_valid_pack_zip(),
        uploaded_by=uuid.uuid4(),
    )

    retry_key = f"resource-packs/{pack.id.value}/{_FILENAME}"
    assert retry_key != orphan_key
    assert set(raw.objects) == {orphan_key, retry_key}
    assert list(uow.resource_packs.packs) == [pack.id]
