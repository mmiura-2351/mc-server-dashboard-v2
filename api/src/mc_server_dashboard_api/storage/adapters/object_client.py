"""The aioboto3-backed :class:`~.object_store.S3Client` (STORAGE.md Section 7.3).

The single module that imports the S3 client library, so the dependency stays at
the very edge: :class:`ObjectStorage` depends only on the narrow
:class:`~.object_store.S3Client` protocol and is exercised in tests against an
in-memory stub. ``aioboto3``/``aiobotocore``/``botocore`` ship no type stubs;
their imports are treated as untyped via the mypy override in ``pyproject.toml``
(the grpcio precedent).

A new aioboto3 client is opened per operation (one ``async with`` per Port call),
which is how :func:`make_s3_client_factory` returns a context manager: the session
is cheap and this keeps connection lifetime scoped to the call, matching the
adapter's per-operation ``client_factory()`` usage.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aioboto3
from botocore.exceptions import ClientError

from mc_server_dashboard_api.storage.adapters.object_store import (
    MultipartUploadsUnsupportedError,
    S3Client,
    S3ClientFactory,
    S3MultipartUpload,
    S3Object,
)
from mc_server_dashboard_api.storage.domain.errors import NotFoundError

# Stream/multipart chunk size: 8 MiB respects the S3 5 MiB minimum for non-final
# multipart parts while keeping per-call memory bounded.
_PART = 8 * 1024 * 1024


class _Aioboto3S3Client:
    """Implements :class:`S3Client` over one aioboto3 client and a fixed bucket."""

    def __init__(self, client: Any, bucket: str) -> None:
        # ``client`` is an aioboto3 S3 client (untyped: aiobotocore ships no stubs,
        # handled by the pyproject mypy override). ``Any`` keeps the dynamic S3
        # method surface usable while the public methods below restore the types.
        self._client = client
        self._bucket = bucket

    async def get_object(self, key: str) -> AsyncIterator[bytes]:
        try:
            resp = await self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                raise NotFoundError(f"object not found: {key}") from exc
            raise
        return _iter_body(resp["Body"])

    async def put_object(self, key: str, body: bytes) -> None:
        await self._client.put_object(Bucket=self._bucket, Key=key, Body=body)

    async def upload_multipart(self, key: str, parts: AsyncIterator[bytes]) -> None:
        # Accumulate into >= _PART buffers so each uploaded part respects the S3
        # minimum, without ever holding the whole object: at most one part plus one
        # incoming chunk in memory.
        created = await self._client.create_multipart_upload(
            Bucket=self._bucket, Key=key
        )
        upload_id = created["UploadId"]
        completed: list[dict[str, object]] = []
        buffer = bytearray()
        part_number = 1
        try:
            async for chunk in parts:
                buffer.extend(chunk)
                if len(buffer) >= _PART:
                    completed.append(
                        await self._upload_part(key, upload_id, part_number, buffer)
                    )
                    part_number += 1
                    buffer = bytearray()
            if buffer or part_number == 1:
                # Always upload at least one (possibly empty) part so an empty
                # object completes rather than aborting on a zero-part upload.
                completed.append(
                    await self._upload_part(key, upload_id, part_number, buffer)
                )
            await self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": completed},
            )
        except BaseException:
            await self._client.abort_multipart_upload(
                Bucket=self._bucket, Key=key, UploadId=upload_id
            )
            raise

    async def _upload_part(
        self, key: str, upload_id: str, part_number: int, data: bytearray
    ) -> dict[str, object]:
        resp = await self._client.upload_part(
            Bucket=self._bucket,
            Key=key,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=bytes(data),
        )
        return {"ETag": resp["ETag"], "PartNumber": part_number}

    async def head_object(self, key: str) -> int | None:
        try:
            resp = await self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                return None
            raise
        size: int = resp["ContentLength"]
        return size

    async def copy_object(self, src_key: str, dst_key: str) -> None:
        await self._client.copy_object(
            Bucket=self._bucket,
            Key=dst_key,
            CopySource={"Bucket": self._bucket, "Key": src_key},
        )

    async def delete_object(self, key: str) -> None:
        await self._client.delete_object(Bucket=self._bucket, Key=key)

    async def list_objects(self, prefix: str) -> list[S3Object]:
        paginator = self._client.get_paginator("list_objects_v2")
        out: list[S3Object] = []
        async for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for entry in page.get("Contents", []):
                # S3 ``LastModified`` is a timezone-aware datetime (UTC); the JAR-pool
                # GC safety window reads it through S3Object (#293).
                out.append(
                    S3Object(
                        key=entry["Key"],
                        size=entry["Size"],
                        last_modified=entry["LastModified"],
                    )
                )
        return out

    async def list_multipart_uploads(self, prefix: str) -> list[S3MultipartUpload]:
        # Paginate ListMultipartUploads so a crash-leftover orphan upload is found
        # regardless of count (issue #903). A backend without the operation (e.g. a
        # SeaweedFS build that lacks it) raises a ClientError the adapter degrades on,
        # so map the unsupported-operation codes to MultipartUploadsUnsupportedError.
        paginator = self._client.get_paginator("list_multipart_uploads")
        out: list[S3MultipartUpload] = []
        try:
            async for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for entry in page.get("Uploads", []):
                    key = entry["Key"]
                    upload_id = entry["UploadId"]
                    # ``Initiated`` is a timezone-aware datetime (UTC); the sweep's
                    # age threshold reads it through S3MultipartUpload. It is OPTIONAL
                    # in the S3 ListMultipartUploads response and SeaweedFS 4.33 omits
                    # it (issue #702/#934 validation), so reading it unconditionally
                    # would crash the startup sweep. Real S3/MinIO supply it; when it
                    # is absent, derive an effective age from the upload's parts
                    # (below) so orphan reclamation stays LIVE on SeaweedFS rather
                    # than relying on an unenforced lifecycle rule.
                    initiated = entry.get("Initiated")
                    if initiated is None:
                        initiated = await self._effective_initiated(key, upload_id)
                    out.append(
                        S3MultipartUpload(
                            key=key, upload_id=upload_id, initiated=initiated
                        )
                    )
        except ClientError as exc:
            if _is_unsupported(exc):
                raise MultipartUploadsUnsupportedError(
                    "object store rejected ListMultipartUploads"
                ) from exc
            raise
        return out

    async def _effective_initiated(self, key: str, upload_id: str) -> dt.datetime:
        # SeaweedFS omits ``Initiated`` from ListMultipartUploads but does return a
        # per-part ``LastModified`` from ListParts (verified against 4.33). Use the
        # NEWEST part's LastModified as the upload's effective initiation time so the
        # sweep's age guard aborts a genuine crash-orphan while never touching a still
        # actively-progressing upload (its newest part is recent).
        #
        # A just-initiated upload with ZERO parts has no LastModified to read; treat
        # that conservatively as "now" so it is never aborted. Residual micro-gap: an
        # upload that crashes after CreateMultipartUpload but before its first
        # UploadPart has no parts and no Initiated, so the sweep never reclaims it.
        # Such an entry holds no part bytes; ``weed shell s3.clean.uploads`` is the
        # SeaweedFS-native operator backstop for that case.
        paginator = self._client.get_paginator("list_parts")
        newest: dt.datetime | None = None
        try:
            async for page in paginator.paginate(
                Bucket=self._bucket, Key=key, UploadId=upload_id
            ):
                for part in page.get("Parts", []):
                    last_modified = part.get("LastModified")
                    if last_modified is not None and (
                        newest is None or last_modified > newest
                    ):
                        newest = last_modified
        except ClientError as exc:
            # The upload completed/aborted between ListMultipartUploads and this
            # ListParts: real S3 raises NoSuchUpload. Treat it as "now" so the
            # vanished upload is never aborted this sweep, rather than letting the
            # error crash the startup sweep — mirroring abort's idempotent handling.
            if _is_no_such_upload(exc):
                return dt.datetime.now(dt.UTC)
            raise
        return newest if newest is not None else dt.datetime.now(dt.UTC)

    async def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        # Idempotent (issue #916): real S3/MinIO raise NoSuchUpload for an already
        # aborted/completed upload id, but the Port documents abort as a no-op in
        # that case (the fake honours it via pop(..., None)). A complete-vs-abort
        # race in a (future periodic) sweep would otherwise crash here, so translate
        # NoSuchUpload to a no-op — mirroring the _is_not_found pattern above.
        try:
            await self._client.abort_multipart_upload(
                Bucket=self._bucket, Key=key, UploadId=upload_id
            )
        except ClientError as exc:
            if _is_no_such_upload(exc):
                return
            raise


def _is_not_found(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code")
    return code in ("404", "NoSuchKey", "NotFound")


def _is_no_such_upload(exc: ClientError) -> bool:
    # An already aborted/completed multipart upload id (issue #916): abort is a
    # no-op in that case, matching the Port's idempotent-abort contract.
    code = exc.response.get("Error", {}).get("Code")
    return code in ("NoSuchUpload",)


def _is_unsupported(exc: ClientError) -> bool:
    # A store that lacks ListMultipartUploads rejects it with one of these codes
    # (issue #903); the sweep degrades to the lifecycle-rule WARN rather than failing.
    code = exc.response.get("Error", {}).get("Code")
    return code in ("NotImplemented", "MethodNotAllowed", "501", "405")


async def _iter_body(body: Any) -> AsyncIterator[bytes]:
    # aiobotocore's StreamingBody exposes an async ``read(n)``; read bounded chunks
    # so a large object never lands in memory whole.
    try:
        while True:
            chunk = await body.read(_PART)
            if not chunk:
                return
            yield chunk
    finally:
        body.close()


def make_s3_client_factory(
    *, endpoint: str, bucket: str, access_key: str, secret_key: str
) -> S3ClientFactory:
    """Build an :class:`S3ClientFactory` over an aioboto3 session.

    See STORAGE.md Section 7.3. Each returned context manager opens an S3 client
    against the configured S3-compatible endpoint and yields a bucket-scoped
    :class:`S3Client`.
    """

    session = aioboto3.Session(
        aws_access_key_id=access_key, aws_secret_access_key=secret_key
    )

    @asynccontextmanager
    async def _factory() -> AsyncIterator[S3Client]:
        async with session.client("s3", endpoint_url=endpoint) as client:
            yield _Aioboto3S3Client(client, bucket)

    return _factory
