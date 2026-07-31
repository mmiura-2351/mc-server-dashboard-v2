"""Unit tests for the real aioboto3-backed S3 client (STORAGE.md Section 7.3).

The adapter's behaviour is proven against the in-memory stub elsewhere; this file
pins the thin error-translation seams in :mod:`...adapters.object_client` that the
stub cannot exercise (they live where the real client raises botocore errors). No
real cloud / moto: a minimal client double raises the exact ``ClientError`` shapes
the production code translates.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator, Awaitable, Callable

import aiohttp
import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    IncompleteReadError,
    ReadTimeoutError,
    ResponseStreamingError,
)

from mc_server_dashboard_api.storage.adapters.object_client import (
    _Aioboto3S3Client,
    _iter_body,
    make_s3_client_factory,
)
from mc_server_dashboard_api.storage.domain.errors import (
    NotFoundError,
    ObjectStoreUnavailableError,
)


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, "AbortMultipartUpload")


class _PagesPaginator:
    """A botocore-paginator double yielding one page with a fixed key/value."""

    def __init__(self, page_key: str, items: list[dict[str, object]]) -> None:
        self._page_key = page_key
        self._items = items

    async def _pages(self) -> AsyncIterator[dict[str, object]]:
        yield {self._page_key: self._items}

    def paginate(self, **_kwargs: object) -> AsyncIterator[dict[str, object]]:
        return self._pages()


class _ListUploadsClient:
    """An aioboto3-client double serving ListMultipartUploads + ListParts pages.

    ``parts`` maps an upload id to its ListParts entries so the missing-``Initiated``
    path (SeaweedFS) can be exercised: the adapter falls back to the newest part's
    ``LastModified`` to age-gate the upload.
    """

    def __init__(
        self,
        uploads: list[dict[str, object]],
        parts: dict[str, list[dict[str, object]]] | None = None,
    ) -> None:
        self._uploads = uploads
        self._parts = parts or {}

    def get_paginator(self, name: str) -> _PagesPaginator:
        if name == "list_parts":
            # The double serves one upload at a time in these tests; return its parts.
            entries = next(iter(self._parts.values()), [])
            return _PagesPaginator("Parts", entries)
        return _PagesPaginator("Uploads", self._uploads)


class _RaisingPartsClient:
    """A double whose ListMultipartUploads omits ``Initiated`` and whose ListParts
    raises a set code, simulating the upload vanishing between the two calls."""

    def __init__(self, uploads: list[dict[str, object]], code: str) -> None:
        self._uploads = uploads
        self._code = code

    def get_paginator(self, name: str) -> _PagesPaginator | _RaisingPaginator:
        if name == "list_parts":
            return _RaisingPaginator(self._code)
        return _PagesPaginator("Uploads", self._uploads)


class _RaisingPaginator:
    """A paginator double that raises a ``ClientError`` when iterated."""

    def __init__(self, code: str) -> None:
        self._code = code

    async def _pages(self) -> AsyncIterator[dict[str, object]]:
        raise _client_error(self._code)
        yield {}  # unreachable; makes this an async generator

    def paginate(self, **_kwargs: object) -> AsyncIterator[dict[str, object]]:
        return self._pages()


class _RaisingAbortClient:
    """An aioboto3-client double whose ``abort_multipart_upload`` raises a set code."""

    def __init__(self, code: str) -> None:
        self._code = code
        self.calls = 0

    async def abort_multipart_upload(self, **_kwargs: object) -> None:
        self.calls += 1
        raise _client_error(self._code)


async def test_abort_multipart_upload_swallows_no_such_upload() -> None:
    # Idempotent abort (issue #916): real S3/MinIO raise NoSuchUpload for an already
    # aborted/completed upload id, but the Port documents abort as a no-op there. The
    # real client must translate NoSuchUpload to a no-op (mirroring the fake), so a
    # complete-vs-abort race in a (future periodic) sweep does not crash.
    raising = _RaisingAbortClient("NoSuchUpload")
    client = _Aioboto3S3Client(raising, "bucket")

    await client.abort_multipart_upload("jars/x.jar", "gone")

    assert raising.calls == 1


async def test_abort_multipart_upload_reraises_other_client_errors() -> None:
    # A real failure (e.g. AccessDenied) must NOT be swallowed: only NoSuchUpload is
    # the idempotent no-op (issue #916).
    raising = _RaisingAbortClient("AccessDenied")
    client = _Aioboto3S3Client(raising, "bucket")

    with pytest.raises(ClientError):
        await client.abort_multipart_upload("jars/x.jar", "live")


class _UploadClient:
    """An aioboto3-client double for ``upload_multipart``: complete fails, and the
    cleanup abort raises a set code so the masking behaviour can be pinned."""

    def __init__(self, complete_error: ClientError, abort_code: str) -> None:
        self._complete_error = complete_error
        self._abort_code = abort_code
        self.abort_calls = 0

    async def create_multipart_upload(self, **_kwargs: object) -> dict[str, object]:
        return {"UploadId": "u"}

    async def upload_part(self, **_kwargs: object) -> dict[str, object]:
        return {"ETag": "etag"}

    async def complete_multipart_upload(self, **_kwargs: object) -> None:
        raise self._complete_error

    async def abort_multipart_upload(self, **_kwargs: object) -> None:
        self.abort_calls += 1
        raise _client_error(self._abort_code)


async def _one_part() -> AsyncIterator[bytes]:
    yield b"data"


async def test_upload_multipart_cleanup_abort_no_such_upload_surfaces() -> None:
    # Complete-vs-abort race (issue #935): the original complete failure (e.g. a
    # periodic sweep aborted the upload, so complete returns NoSuchUpload) must
    # surface — NOT be masked by the cleanup abort's own NoSuchUpload. Routing cleanup
    # through the translated idempotent abort makes that abort a no-op, so the original
    # error wins. The escaping type is the translated ObjectStoreUnavailableError
    # (issue #2270), and its ``__cause__`` is the ORIGINAL complete error — proving the
    # cleanup abort did not mask it. ``operation_name`` distinguishes the two errors.
    complete_error = ClientError(
        {"Error": {"Code": "NoSuchUpload"}}, "CompleteMultipartUpload"
    )
    upload = _UploadClient(complete_error, "NoSuchUpload")
    client = _Aioboto3S3Client(upload, "bucket")

    with pytest.raises(ObjectStoreUnavailableError) as excinfo:
        await client.upload_multipart("jars/x.jar", _one_part())

    assert excinfo.value.__cause__ is complete_error
    assert upload.abort_calls == 1


async def test_upload_multipart_cleanup_abort_other_error_does_not_mask() -> None:
    # If the cleanup abort fails with a DIFFERENT error (e.g. AccessDenied), the
    # original upload error must still win — the orphan upload is recoverable by the
    # sweep, masking the cause is not (issue #935). The escaping type is the translated
    # ObjectStoreUnavailableError (issue #2270) whose ``__cause__`` is the original
    # complete error, not the cleanup abort's AccessDenied.
    complete_error = ClientError(
        {"Error": {"Code": "InternalError"}}, "CompleteMultipartUpload"
    )
    upload = _UploadClient(complete_error, "AccessDenied")
    client = _Aioboto3S3Client(upload, "bucket")

    with pytest.raises(ObjectStoreUnavailableError) as excinfo:
        await client.upload_multipart("jars/x.jar", _one_part())

    assert excinfo.value.__cause__ is complete_error
    assert upload.abort_calls == 1


class _UploadPartRaisingClient:
    """An upload_multipart double whose ``upload_part`` raises a set error.

    ``create_multipart_upload`` and the cleanup ``abort_multipart_upload`` succeed, so
    the error under test is exactly the ``UploadPart`` failure the adapter must
    translate (issue #2270 — the 2026-07-23 SeaweedFS ``UploadPart`` incident)."""

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.abort_calls = 0

    async def create_multipart_upload(self, **_kwargs: object) -> dict[str, object]:
        return {"UploadId": "u"}

    async def upload_part(self, **_kwargs: object) -> dict[str, object]:
        raise self._error

    async def abort_multipart_upload(self, **_kwargs: object) -> None:
        self.abort_calls += 1


async def test_upload_multipart_translates_client_error_on_upload_part() -> None:
    # The 2026-07-23 incident: SeaweedFS returns an HTTP 500 ``InternalError`` on
    # UploadPart. The raw botocore ``ClientError`` must NOT cross the Storage Port
    # boundary (the object-store adapter's contract): it is translated to
    # ObjectStoreUnavailableError so upstream receives a typed, categorizable storage
    # error instead of an untranslated third-party exception. The original error is
    # preserved as the cause, and the orphan upload is cleaned up via the abort.
    error = ClientError({"Error": {"Code": "InternalError"}}, "UploadPart")
    upload = _UploadPartRaisingClient(error)
    client = _Aioboto3S3Client(upload, "bucket")

    with pytest.raises(ObjectStoreUnavailableError) as excinfo:
        await client.upload_multipart("communities/k/backups/x.tar.gz", _one_part())

    assert excinfo.value.__cause__ is error
    assert upload.abort_calls == 1


@pytest.mark.parametrize(
    "error",
    [
        EndpointConnectionError(endpoint_url="http://store:8333"),
        ConnectTimeoutError(endpoint_url="http://store:8333"),
        ReadTimeoutError(endpoint_url="http://store:8333"),
    ],
)
async def test_upload_multipart_translates_transport_errors(
    error: BaseException,
) -> None:
    # A connection/timeout transport failure mid-upload is likewise a botocore type
    # that must not cross the boundary (issue #2270): it is translated to
    # ObjectStoreUnavailableError, preserving the transport error as the cause.
    upload = _UploadPartRaisingClient(error)
    client = _Aioboto3S3Client(upload, "bucket")

    with pytest.raises(ObjectStoreUnavailableError) as excinfo:
        await client.upload_multipart("communities/k/backups/x.tar.gz", _one_part())

    assert excinfo.value.__cause__ is error


class _CreateRaisingClient:
    """A double whose ``create_multipart_upload`` raises — the upload never starts."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def create_multipart_upload(self, **_kwargs: object) -> dict[str, object]:
        raise self._error


async def test_upload_multipart_translates_client_error_on_create() -> None:
    # A backend failure initiating the multipart upload (CreateMultipartUpload) is
    # translated too (issue #2270); there is no upload id yet, so no cleanup abort.
    error = ClientError({"Error": {"Code": "InternalError"}}, "CreateMultipartUpload")
    client = _Aioboto3S3Client(_CreateRaisingClient(error), "bucket")

    with pytest.raises(ObjectStoreUnavailableError) as excinfo:
        await client.upload_multipart("communities/k/backups/x.tar.gz", _one_part())

    assert excinfo.value.__cause__ is error


async def test_list_multipart_uploads_reads_initiated_when_present() -> None:
    # The S3 ``Initiated`` timestamp drives the sweep's age threshold; when the
    # backend supplies it, it is read verbatim (issue #903).
    initiated = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    client = _Aioboto3S3Client(
        _ListUploadsClient(
            [{"Key": "communities/k", "UploadId": "u", "Initiated": initiated}]
        ),
        "bucket",
    )

    uploads = await client.list_multipart_uploads("communities/")

    assert len(uploads) == 1
    assert uploads[0].initiated == initiated


async def test_list_multipart_uploads_ages_via_parts_when_initiated_missing() -> None:
    # SeaweedFS (issue #702/#934 validation) returns ListMultipartUploads entries
    # WITHOUT the optional ``Initiated`` field but DOES return per-part
    # ``LastModified`` from ListParts. The adapter must age-gate the upload by the
    # NEWEST part's LastModified so an old crash-orphan is still reclaimed by the
    # sweep on SeaweedFS, rather than relying on an unenforced lifecycle rule.
    older = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    newest = dt.datetime(2026, 1, 2, tzinfo=dt.UTC)
    client = _Aioboto3S3Client(
        _ListUploadsClient(
            [{"Key": "communities/k", "UploadId": "u"}],
            parts={
                "u": [
                    {"PartNumber": 1, "LastModified": older},
                    {"PartNumber": 2, "LastModified": newest},
                ]
            },
        ),
        "bucket",
    )

    uploads = await client.list_multipart_uploads("communities/")

    assert len(uploads) == 1
    assert uploads[0].initiated == newest


async def test_list_multipart_uploads_zero_parts_no_initiated_treated_as_now() -> None:
    # The conservative edge: a just-initiated SeaweedFS upload with ZERO parts has no
    # ``Initiated`` AND no part ``LastModified`` to read. It is treated as "now" so
    # the sweep's age guard never aborts a possibly-live just-started upload. This
    # leaves a documented residual micro-gap (a crash before the first UploadPart is
    # not reclaimed by the sweep; ``weed shell s3.clean.uploads`` is the backstop).
    before = dt.datetime.now(dt.UTC)
    client = _Aioboto3S3Client(
        _ListUploadsClient(
            [{"Key": "communities/k", "UploadId": "u"}], parts={"u": []}
        ),
        "bucket",
    )

    uploads = await client.list_multipart_uploads("communities/")

    assert len(uploads) == 1
    assert uploads[0].initiated.tzinfo is not None
    assert uploads[0].initiated >= before


async def test_list_multipart_uploads_survives_parts_no_such_upload_race() -> None:
    # Defense-in-depth: an upload listed by ListMultipartUploads can complete or abort
    # before the missing-``Initiated`` fallback issues ListParts, on which real S3
    # raises NoSuchUpload. That must NOT crash the startup sweep — the vanished upload
    # is treated as "now" (never aborted this sweep) rather than letting the error
    # propagate, mirroring abort's idempotent NoSuchUpload handling.
    before = dt.datetime.now(dt.UTC)
    client = _Aioboto3S3Client(
        _RaisingPartsClient(
            [{"Key": "communities/k", "UploadId": "u"}], "NoSuchUpload"
        ),
        "bucket",
    )

    uploads = await client.list_multipart_uploads("communities/")

    assert len(uploads) == 1
    assert uploads[0].initiated >= before


class _RaisingObjectClient:
    """A client double whose get_object/head_object raise a set code."""

    def __init__(self, code: str) -> None:
        self._code = code

    async def get_object(self, **_kwargs: object) -> dict[str, object]:
        raise _client_error(self._code)

    async def head_object(self, **_kwargs: object) -> dict[str, object]:
        raise _client_error(self._code)


class _RaisingListClient:
    """A client double whose paginators raise a set code on iteration."""

    def __init__(self, code: str) -> None:
        self._code = code

    def get_paginator(self, _name: str) -> _RaisingPaginator:
        return _RaisingPaginator(self._code)


# --- Bucketless store reads as empty/not-found (issue #946) ------------------
#
# SeaweedFS auto-creates the bucket on first WRITE, so on a fresh deployment every
# READ raises NoSuchBucket before any bucket exists. Empirically (SeaweedFS 4.33):
# ListObjectsV2 / GetObject / ListMultipartUploads / ListParts all surface
# ``NoSuchBucket``; HeadObject surfaces a bare ``404`` (already handled). The read
# paths must treat NoSuchBucket as empty/not-found so the startup sweep — and the
# FastAPI lifespan — boot against a bucketless store, while other errors still raise.


async def test_get_object_no_such_bucket_raises_not_found() -> None:
    client = _Aioboto3S3Client(_RaisingObjectClient("NoSuchBucket"), "bucket")

    with pytest.raises(NotFoundError):
        await client.get_object("communities/k")


async def test_get_object_other_error_reraises() -> None:
    client = _Aioboto3S3Client(_RaisingObjectClient("AccessDenied"), "bucket")

    with pytest.raises(ClientError):
        await client.get_object("communities/k")


async def test_head_object_no_such_bucket_returns_none() -> None:
    client = _Aioboto3S3Client(_RaisingObjectClient("NoSuchBucket"), "bucket")

    assert await client.head_object("communities/k") is None


async def test_list_objects_no_such_bucket_returns_empty() -> None:
    client = _Aioboto3S3Client(_RaisingListClient("NoSuchBucket"), "bucket")

    assert await client.list_objects("communities/") == []


async def test_list_objects_other_error_reraises() -> None:
    client = _Aioboto3S3Client(_RaisingListClient("AccessDenied"), "bucket")

    with pytest.raises(ClientError):
        await client.list_objects("communities/")


async def test_list_multipart_uploads_no_such_bucket_returns_empty() -> None:
    client = _Aioboto3S3Client(_RaisingListClient("NoSuchBucket"), "bucket")

    assert await client.list_multipart_uploads("communities/") == []


async def test_effective_initiated_survives_parts_no_such_bucket_race() -> None:
    # Defense-in-depth uniformity (issue #946): if the bucket vanishes between the
    # ListMultipartUploads that listed an upload and the missing-``Initiated`` fallback
    # ListParts, NoSuchBucket must be treated as "now" (never aborted this sweep)
    # rather than crashing the sweep — mirroring the NoSuchUpload race handling.
    before = dt.datetime.now(dt.UTC)
    client = _Aioboto3S3Client(
        _RaisingPartsClient(
            [{"Key": "communities/k", "UploadId": "u"}], "NoSuchBucket"
        ),
        "bucket",
    )

    uploads = await client.list_multipart_uploads("communities/")

    assert len(uploads) == 1
    assert uploads[0].initiated >= before


# --- copy_object NotFound translation (issue #1953) -----------------------


class _CopyRaisingClient:
    """An aioboto3-client double whose ``copy_object`` raises a ClientError."""

    def __init__(self, code: str) -> None:
        self._code = code

    async def copy_object(self, **_kwargs: object) -> None:
        raise ClientError({"Error": {"Code": self._code}}, "CopyObject")


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
async def test_copy_object_translates_not_found(code: str) -> None:
    """copy_object must translate the same error codes as get_object (#1953)."""

    client = _Aioboto3S3Client(_CopyRaisingClient(code), "bucket")
    with pytest.raises(NotFoundError):
        await client.copy_object("src/key", "dst/key")


# --- Read-path backend-fault translation (issues #2376, #2378) -------------
#
# The write paths translate a backend/transport failure; the read paths did not, so
# an outage at request initiation leaked a raw botocore type across the Storage Port
# (#2376) and made the edge's 503 unreachable on the read routes (#2378).
#
# These drive REAL botocore errors through ``_Aioboto3S3Client``. The seam tests one
# layer up fake an already-typed error on an ``FsStorage`` subclass, which is exactly
# why they cannot catch a regression here.
#
# The read paths use a NARROWER rule than ``_UPLOAD_FAILURE_ERRORS``: a bare
# ``ClientError`` would sweep in a 403 ``AccessDenied``, a standing misconfiguration
# that must stay loud rather than read as "retry".


def _service_error(operation: str, status: int, code: str) -> ClientError:
    """A ``ClientError`` shaped like a real botocore service error.

    botocore populates ``ResponseMetadata.HTTPStatusCode`` from the response it
    parsed; the read paths key on it to tell a backend fault from a refusal.
    """

    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation,
    )


class _RaisingErrorPaginator:
    """A paginator double that raises a set error when iterated."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def _pages(self) -> AsyncIterator[dict[str, object]]:
        raise self._error
        yield {}  # unreachable; makes this an async generator

    def paginate(self, **_kwargs: object) -> AsyncIterator[dict[str, object]]:
        return self._pages()


class _RaisingReadClient:
    """A client double whose read operations all raise a set error."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def get_object(self, **_kwargs: object) -> dict[str, object]:
        raise self._error

    async def head_object(self, **_kwargs: object) -> dict[str, object]:
        raise self._error

    async def copy_object(self, **_kwargs: object) -> None:
        raise self._error

    def get_paginator(self, _name: str) -> _RaisingErrorPaginator:
        return _RaisingErrorPaginator(self._error)


# The four read operations that reach the store before a response body starts, each
# with the call shape its test needs.
_READ_CALLS: dict[str, Callable[[_Aioboto3S3Client], Awaitable[object]]] = {
    "get_object": lambda c: c.get_object("communities/k"),
    "head_object": lambda c: c.head_object("communities/k"),
    "copy_object": lambda c: c.copy_object("src/k", "dst/k"),
    "list_objects": lambda c: c.list_objects("communities/"),
}


@pytest.mark.parametrize("operation", sorted(_READ_CALLS))
async def test_read_translates_backend_5xx(operation: str) -> None:
    # The store answered, but with a fault of its own (the SeaweedFS HTTP 500
    # ``InternalError`` shape of the 2026-07-23 incident, on a read this time). That is
    # the transient condition the edge reports as 503, so it must not cross the Port
    # as a raw botocore type.
    error = _service_error("GetObject", 500, "InternalError")
    client = _Aioboto3S3Client(_RaisingReadClient(error), "bucket")

    with pytest.raises(ObjectStoreUnavailableError) as excinfo:
        await _READ_CALLS[operation](client)

    assert excinfo.value.__cause__ is error


@pytest.mark.parametrize("operation", sorted(_READ_CALLS))
@pytest.mark.parametrize(
    "error",
    [
        EndpointConnectionError(endpoint_url="http://store:8333"),
        ConnectTimeoutError(endpoint_url="http://store:8333"),
        ReadTimeoutError(endpoint_url="http://store:8333"),
    ],
)
async def test_read_translates_transport_errors(
    operation: str, error: BaseException
) -> None:
    # The request never got a usable response back — connection refused, or a
    # connect/read timeout at request initiation (issue #2376). Same verdict.
    client = _Aioboto3S3Client(_RaisingReadClient(error), "bucket")

    with pytest.raises(ObjectStoreUnavailableError) as excinfo:
        await _READ_CALLS[operation](client)

    assert excinfo.value.__cause__ is error


@pytest.mark.parametrize("operation", sorted(_READ_CALLS))
async def test_read_reraises_non_transient_refusal(operation: str) -> None:
    # The care issue #2376 names: a ``ClientError`` carrying real semantics must keep
    # them. A 403 ``AccessDenied`` is a standing credential/policy misconfiguration —
    # retrying it never succeeds, so classifying it as a backend outage would both
    # mislead the operator and tell every client to retry forever.
    error = _service_error("GetObject", 403, "AccessDenied")
    client = _Aioboto3S3Client(_RaisingReadClient(error), "bucket")

    with pytest.raises(ClientError):
        await _READ_CALLS[operation](client)


# --- Single-object write translation (issue #2273) -------------------------
#
# put_object / delete_object are single-object writes (no multipart abort to run),
# so they are simpler than upload_multipart but carry the same Storage Port contract:
# a botocore transport/backend failure must not cross the boundary as a raw type, so
# it is translated to ObjectStoreUnavailableError with the original as the ``__cause__``
# -- mirroring the upload_multipart translation (#2270). A botocore *usage* error is
# deliberately NOT caught (it is excluded from ``_UPLOAD_FAILURE_ERRORS``).


class _RaisingWriteClient:
    """A client double whose put_object/delete_object raise a set error."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def put_object(self, **_kwargs: object) -> None:
        raise self._error

    async def delete_object(self, **_kwargs: object) -> None:
        raise self._error


async def test_put_object_translates_client_error() -> None:
    # A backend service failure on PutObject (e.g. a SeaweedFS HTTP 500) must not cross
    # the Storage Port as a raw botocore type; it is translated (issue #2273).
    error = ClientError({"Error": {"Code": "InternalError"}}, "PutObject")
    client = _Aioboto3S3Client(_RaisingWriteClient(error), "bucket")

    with pytest.raises(ObjectStoreUnavailableError) as excinfo:
        await client.put_object("communities/k/x", b"data")

    assert excinfo.value.__cause__ is error


async def test_delete_object_translates_client_error() -> None:
    # A backend service failure on DeleteObject is translated too (issue #2273).
    error = ClientError({"Error": {"Code": "InternalError"}}, "DeleteObject")
    client = _Aioboto3S3Client(_RaisingWriteClient(error), "bucket")

    with pytest.raises(ObjectStoreUnavailableError) as excinfo:
        await client.delete_object("communities/k/x")

    assert excinfo.value.__cause__ is error


@pytest.mark.parametrize(
    "error",
    [
        EndpointConnectionError(endpoint_url="http://store:8333"),
        ConnectTimeoutError(endpoint_url="http://store:8333"),
        ReadTimeoutError(endpoint_url="http://store:8333"),
    ],
)
async def test_put_object_translates_transport_errors(error: BaseException) -> None:
    # A connection/timeout transport failure on PutObject is a botocore type that must
    # not cross the boundary (issue #2273); it is translated with the error as cause.
    client = _Aioboto3S3Client(_RaisingWriteClient(error), "bucket")

    with pytest.raises(ObjectStoreUnavailableError) as excinfo:
        await client.put_object("communities/k/x", b"data")

    assert excinfo.value.__cause__ is error


@pytest.mark.parametrize(
    "error",
    [
        EndpointConnectionError(endpoint_url="http://store:8333"),
        ConnectTimeoutError(endpoint_url="http://store:8333"),
        ReadTimeoutError(endpoint_url="http://store:8333"),
    ],
)
async def test_delete_object_translates_transport_errors(error: BaseException) -> None:
    # A connection/timeout transport failure on DeleteObject is likewise translated
    # (issue #2273), preserving the transport error as the cause.
    client = _Aioboto3S3Client(_RaisingWriteClient(error), "bucket")

    with pytest.raises(ObjectStoreUnavailableError) as excinfo:
        await client.delete_object("communities/k/x")

    assert excinfo.value.__cause__ is error


# --- Explicit, settings-sourced timeouts + retries (issue #2249) -----------


async def test_factory_builds_client_with_settings_sourced_timeouts_and_retries() -> (
    None
):
    # The built client must carry the explicit, settings-sourced transport budget
    # rather than inheriting botocore's hidden defaults (60s connect/read + legacy
    # retries). Inspect the real client's resolved ``meta.config`` so the assertion
    # pins the EFFECTIVE values botocore will use — no network call is made (the
    # context manager only builds the client). ``retry_max_attempts`` is the TOTAL
    # attempt count: botocore normalizes the retries config to ``total_max_attempts``,
    # and this must equal the field verbatim (N means N attempts, not N + 1). This is
    # the assertion that catches the ``max_attempts`` vs ``total_max_attempts``
    # off-by-one.
    factory = make_s3_client_factory(
        endpoint="http://localhost:8333",
        bucket="bucket",
        access_key="ak",
        secret_key="sk",
        connect_timeout=7.0,
        read_timeout=42.0,
        retry_max_attempts=3,
    )
    async with factory() as s3:
        assert isinstance(s3, _Aioboto3S3Client)
        config = s3._client.meta.config
        assert config.connect_timeout == 7.0
        assert config.read_timeout == 42.0
        assert config.retries["mode"] == "standard"
        assert config.retries["total_max_attempts"] == 3


# --- Body-read translation (issue #2371) ------------------------------------
#
# The backup readability probe can only tell a store outage from damaged bytes if a
# body torn down mid-stream arrives as a typed storage outcome. These tests pin that
# against the shapes the REAL stack raises, verified end to end by the socket test
# at the bottom: an enumeration of botocore's ``ClientError`` / ``ConnectionError`` /
# ``HTTPClientError`` catches NONE of the short-body shapes, because aiohttp's
# ``ClientPayloadError`` is a sibling of ``ClientConnectionError`` (so aiobotocore's
# ``StreamingBody.read`` never maps it) and botocore's ``IncompleteReadError`` is a
# bare ``BotoCoreError``.


class _RaisingBody:
    """An aiobotocore ``StreamingBody`` double whose ``read`` fails after one chunk."""

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self._served = False
        self.closed = False

    async def read(self, _amt: int) -> bytes:
        if not self._served:
            self._served = True
            return b"payload"
        raise self._error

    def close(self) -> None:
        self.closed = True


async def _drain_body(body: _RaisingBody) -> int:
    return sum([len(chunk) async for chunk in _iter_body(body)])


@pytest.mark.parametrize(
    "error",
    [
        # What the real stack raises when a body ends short of its declared
        # Content-Length: aiohttp's parser raises ContentLengthError at feed_eof and
        # surfaces ClientPayloadError, which aiobotocore does not map.
        aiohttp.ClientPayloadError("Response payload is not completed"),
        # aiobotocore's own short-body signal, raised by _verify_content_length when
        # the stream reaches EOF having read fewer bytes than Content-Length declared.
        IncompleteReadError(actual_bytes=40000, expected_bytes=100000000),
        # The one shape aiobotocore DOES map (a dropped connection); kept so the
        # already-covered path cannot regress while the new ones are added.
        ResponseStreamingError(error="connection reset"),
    ],
    ids=["aiohttp-payload", "botocore-incomplete-read", "response-streaming"],
)
async def test_body_read_failure_is_translated(error: BaseException) -> None:
    body = _RaisingBody(error)

    with pytest.raises(ObjectStoreUnavailableError) as excinfo:
        await _drain_body(body)

    assert excinfo.value.__cause__ is error
    assert body.closed  # the body is still released on the failure path.


async def test_short_body_over_a_real_socket_is_translated() -> None:
    """End-to-end through the REAL aioboto3/aiobotocore/aiohttp stack: a server that
    declares a large ``Content-Length`` and tears the connection down partway through
    the body — the reported deployment signature (issue #2371).

    This is the test that would have caught the round-1 defect, where the translation
    enumerated botocore types and the raw ``aiohttp.ClientPayloadError`` escaped. It
    drives the actual client rather than a double, so it stays honest if aiobotocore's
    or aiohttp's exception taxonomy shifts under us.
    """

    declared = 100_000_000
    delivered = 40_000

    async def _handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        while True:  # drain the request headers
            line = await reader.readline()
            if line in (b"\r\n", b"", b"\n"):
                break
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/octet-stream\r\n"
            + f"Content-Length: {declared}\r\n".encode()
            + b"\r\n"
        )
        writer.write(b"\x1f\x8b" + b"A" * (delivered - 2))
        await writer.drain()
        writer.close()  # tear the body down well short of Content-Length

    server = await asyncio.start_server(_handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        factory = make_s3_client_factory(
            endpoint=f"http://127.0.0.1:{port}",
            bucket="bucket",
            access_key="ak",
            secret_key="sk",
            connect_timeout=5.0,
            read_timeout=30.0,
            retry_max_attempts=1,
        )
        read = 0
        with pytest.raises(ObjectStoreUnavailableError):
            async with factory() as client:
                async for chunk in await client.get_object("communities/k/x.tar.gz"):
                    read += len(chunk)
        # The bytes the store DID deliver reached the caller before the teardown —
        # that partial count is what the readability probe classifies on.
        assert read == delivered
    finally:
        server.close()
        await server.wait_closed()
