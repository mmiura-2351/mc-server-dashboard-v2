"""A minimal in-memory S3 stub for the object-adapter tests (STORAGE.md §7.3).

Implements exactly the :class:`~...adapters.object_store.S3Client` surface the
adapter uses, backed by a shared ``dict`` of key -> bytes. A single store is
shared across every client the factory hands out (one bucket), so reads see
prior writes (read-after-write) and the pointer-flip / staged-upload / sweep
behaviour can be proven without any real cloud.

moto was rejected in favour of this stub: moto pulls in the full boto3/botocore
+ werkzeug/responses dependency tree and a heavier startup, for behaviour this
test suite only needs a handful of object ops to exercise. A ~70-line dict-backed
stub is simpler, has no extra dependency to pin under the 7-day cooldown, and
makes the failure-injection seams (used by the crash-safety tests) trivial.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mc_server_dashboard_api.storage.adapters.object_store import (
    S3Client,
    S3ClientFactory,
    S3Object,
)
from mc_server_dashboard_api.storage.domain.errors import NotFoundError


class FakeS3Store:
    """The shared bucket contents: an ordered key -> bytes map.

    ``multipart_parts`` records, per key, how many parts the last multipart upload
    consumed. A client-side regression that buffers a stream whole before uploading
    would record a single part, so the per-part streaming contract (Section 7.3) is
    observable in tests.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.multipart_parts: dict[str, int] = {}


class FakeS3Client:
    """An :class:`S3Client` over a shared :class:`FakeS3Store`."""

    def __init__(self, store: FakeS3Store) -> None:
        self._store = store

    async def get_object(self, key: str) -> AsyncIterator[bytes]:
        if key not in self._store.objects:
            raise NotFoundError(f"object not found: {key}")
        data = self._store.objects[key]

        async def _gen() -> AsyncIterator[bytes]:
            # Yield in two chunks when large enough so streaming consumers see
            # more than one yield (the bounded-memory contract).
            half = len(data) // 2
            if half:
                yield data[:half]
                yield data[half:]
            else:
                yield data

        return _gen()

    async def put_object(self, key: str, body: bytes) -> None:
        self._store.objects[key] = bytes(body)

    async def upload_multipart(self, key: str, parts: AsyncIterator[bytes]) -> None:
        # Consume part-by-part (never a single whole-body read) and tally the parts,
        # so a client that buffers the stream whole — collapsing to one part — is
        # caught by the bounded-memory assertion in the tests (Section 7.3).
        buf = bytearray()
        count = 0
        async for chunk in parts:
            buf.extend(chunk)
            count += 1
        self._store.objects[key] = bytes(buf)
        self._store.multipart_parts[key] = count

    async def head_object(self, key: str) -> int | None:
        obj = self._store.objects.get(key)
        return None if obj is None else len(obj)

    async def copy_object(self, src_key: str, dst_key: str) -> None:
        if src_key not in self._store.objects:
            raise NotFoundError(f"object not found: {src_key}")
        self._store.objects[dst_key] = self._store.objects[src_key]

    async def delete_object(self, key: str) -> None:
        self._store.objects.pop(key, None)

    async def list_objects(self, prefix: str) -> list[S3Object]:
        return [
            S3Object(key=key, size=len(data))
            for key, data in sorted(self._store.objects.items())
            if key.startswith(prefix)
        ]


def fake_s3_factory(store: FakeS3Store) -> S3ClientFactory:
    """A client factory yielding fresh :class:`FakeS3Client`s over one store."""

    @asynccontextmanager
    async def _factory() -> AsyncIterator[S3Client]:
        yield FakeS3Client(store)

    return _factory
