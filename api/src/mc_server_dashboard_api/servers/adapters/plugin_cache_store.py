"""Object-store implementation of the ``PluginCacheStore`` Port (issue #1306).

Stores cached jar blobs under the ``plugin-cache/<sha256>`` key namespace
(top-level, outside ``communities/``), keyed by the SHA-256 content address.
Uses the same :class:`~...storage.adapters.object_store.S3ClientFactory` as the
main ``ObjectStorage`` and ``ObjectResourcePackStore`` adapters.

Dedup-on-ingest: :meth:`put` ``head_object``-checks the content key first and
skips the upload when the blob already exists, so identical bytes land once.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from mc_server_dashboard_api.servers.domain.plugin_cache_store import (
    PluginCacheStore,
)
from mc_server_dashboard_api.storage.adapters.object_store import S3ClientFactory
from mc_server_dashboard_api.storage.domain.errors import NotFoundError


def _key(sha256: str) -> str:
    return f"plugin-cache/{sha256}"


class ObjectPluginCacheStore(PluginCacheStore):
    """:class:`PluginCacheStore` adapter over an S3-compatible object store."""

    def __init__(self, client_factory: S3ClientFactory) -> None:
        self._client_factory = client_factory

    async def has(self, sha256: str) -> bool:
        async with self._client_factory() as client:
            return await client.head_object(_key(sha256)) is not None

    async def put(self, sha256: str, stream: AsyncIterator[bytes]) -> None:
        key = _key(sha256)
        async with self._client_factory() as client:
            # Dedup-on-ingest: identical content addresses the same key, so skip
            # the upload when the blob is already cached.
            if await client.head_object(key) is None:
                await client.upload_multipart(key, stream)

    def open(self, sha256: str) -> AsyncIterator[bytes]:
        return self._open_gen(sha256)

    async def _open_gen(self, sha256: str) -> AsyncIterator[bytes]:
        key = _key(sha256)
        async with self._client_factory() as client:
            if await client.head_object(key) is None:
                raise NotFoundError(f"cached jar not found: {sha256}")
            async for chunk in await client.get_object(key):
                yield chunk
