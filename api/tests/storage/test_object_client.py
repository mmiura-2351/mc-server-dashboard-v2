"""Unit tests for the real aioboto3-backed S3 client (STORAGE.md Section 7.3).

The adapter's behaviour is proven against the in-memory stub elsewhere; this file
pins the thin error-translation seams in :mod:`...adapters.object_client` that the
stub cannot exercise (they live where the real client raises botocore errors). No
real cloud / moto: a minimal client double raises the exact ``ClientError`` shapes
the production code translates.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from mc_server_dashboard_api.storage.adapters.object_client import _Aioboto3S3Client


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, "AbortMultipartUpload")


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
