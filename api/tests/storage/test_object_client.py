"""Unit tests for the real aioboto3-backed S3 client (STORAGE.md Section 7.3).

The adapter's behaviour is proven against the in-memory stub elsewhere; this file
pins the thin error-translation seams in :mod:`...adapters.object_client` that the
stub cannot exercise (they live where the real client raises botocore errors). No
real cloud / moto: a minimal client double raises the exact ``ClientError`` shapes
the production code translates.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

import pytest
from botocore.exceptions import ClientError

from mc_server_dashboard_api.storage.adapters.object_client import _Aioboto3S3Client


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
    # error wins. ``operation_name`` distinguishes the two errors below.
    complete_error = ClientError(
        {"Error": {"Code": "NoSuchUpload"}}, "CompleteMultipartUpload"
    )
    upload = _UploadClient(complete_error, "NoSuchUpload")
    client = _Aioboto3S3Client(upload, "bucket")

    with pytest.raises(ClientError) as excinfo:
        await client.upload_multipart("jars/x.jar", _one_part())

    assert excinfo.value is complete_error
    assert upload.abort_calls == 1


async def test_upload_multipart_cleanup_abort_other_error_does_not_mask() -> None:
    # If the cleanup abort fails with a DIFFERENT error (e.g. AccessDenied), the
    # original upload error must still win — the orphan upload is recoverable by the
    # sweep, masking the cause is not (issue #935).
    complete_error = ClientError(
        {"Error": {"Code": "InternalError"}}, "CompleteMultipartUpload"
    )
    upload = _UploadClient(complete_error, "AccessDenied")
    client = _Aioboto3S3Client(upload, "bucket")

    with pytest.raises(ClientError) as excinfo:
        await client.upload_multipart("jars/x.jar", _one_part())

    assert excinfo.value is complete_error
    assert upload.abort_calls == 1


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
