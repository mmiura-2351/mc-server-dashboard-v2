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


class _ListUploadsPaginator:
    """A botocore-paginator double yielding one page of ``Uploads`` entries."""

    def __init__(self, uploads: list[dict[str, object]]) -> None:
        self._uploads = uploads

    async def _pages(self) -> AsyncIterator[dict[str, object]]:
        yield {"Uploads": self._uploads}

    def paginate(self, **_kwargs: object) -> AsyncIterator[dict[str, object]]:
        return self._pages()


class _ListUploadsClient:
    """An aioboto3-client double whose paginator returns set ``Uploads`` entries."""

    def __init__(self, uploads: list[dict[str, object]]) -> None:
        self._uploads = uploads

    def get_paginator(self, _name: str) -> _ListUploadsPaginator:
        return _ListUploadsPaginator(self._uploads)


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


async def test_list_multipart_uploads_tolerates_missing_initiated() -> None:
    # SeaweedFS (issue #702 validation) returns ListMultipartUploads entries WITHOUT
    # the optional ``Initiated`` field, which would crash the sweep with a KeyError.
    # A missing timestamp is treated as "just now" so the sweep's age guard never
    # aborts the upload — orphan reclamation degrades to the lifecycle rule, the same
    # posture as the unsupported-operation path.
    before = dt.datetime.now(dt.UTC)
    client = _Aioboto3S3Client(
        _ListUploadsClient([{"Key": "communities/k", "UploadId": "u"}]),
        "bucket",
    )

    uploads = await client.list_multipart_uploads("communities/")

    assert len(uploads) == 1
    assert uploads[0].initiated.tzinfo is not None
    assert uploads[0].initiated >= before
