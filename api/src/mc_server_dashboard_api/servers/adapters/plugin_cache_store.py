"""Object-store implementation of the ``PluginCacheStore`` Port (issue #1306).

Stores cached jar blobs under the ``plugin-cache/<sha256>`` key namespace
(top-level, outside ``communities/``), keyed by the SHA-256 content address.
Uses the same :class:`~...storage.adapters.object_store.S3ClientFactory` as the
main ``ObjectStorage`` and ``ObjectResourcePackStore`` adapters.

Dedup-on-ingest: :meth:`put` ``head_object``-checks the content key first and
skips the upload when the blob already exists, so identical bytes land once.

The seam translates the storage error so no storage type crosses back into the
servers layer (mirroring ``backup_store.py`` and ``resource_pack_store.py``): a
missing blob surfaces as :class:`PluginCacheBlobNotFoundError` (issue #2338), and
a store outage on any of the four calls as
:class:`PluginCacheStorageUnavailableError` (issue #3233).

What an interrupted :meth:`put` leaves behind is at most the blob itself: the
object client aborts the multipart upload before it reports the outage, and a
completion whose response was lost leaves the completed blob under its content
key. Either way a retry is safe -- the key is the content's own address, so the
retry finds the blob and skips the upload, or uploads the same bytes again.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from mc_server_dashboard_api.servers.domain.errors import (
    PluginCacheBlobNotFoundError,
    PluginCacheStorageUnavailableError,
)
from mc_server_dashboard_api.servers.domain.plugin_cache_store import (
    CacheEntry,
    PluginCacheStore,
)
from mc_server_dashboard_api.storage.adapters.object_store import (
    S3ClientFactory,
    shielded_exit,
)
from mc_server_dashboard_api.storage.domain.errors import (
    NotFoundError,
    ObjectStoreUnavailableError,
)


def _key(sha256: str) -> str:
    return f"plugin-cache/{sha256}"


class ObjectPluginCacheStore(PluginCacheStore):
    """:class:`PluginCacheStore` adapter over an S3-compatible object store."""

    def __init__(self, client_factory: S3ClientFactory) -> None:
        self._client_factory = client_factory

    async def put(self, sha256: str, stream: AsyncIterator[bytes]) -> None:
        key = _key(sha256)
        try:
            async with self._client_factory() as client:
                # Dedup-on-ingest: identical content addresses the same key, so
                # skip the upload when the blob is already cached.
                if await client.head_object(key) is None:
                    await client.upload_multipart(key, stream)
        except ObjectStoreUnavailableError as exc:
            raise PluginCacheStorageUnavailableError(key) from exc

    def open(self, sha256: str) -> AsyncIterator[bytes]:
        return self._open_gen(sha256)

    async def _open_gen(self, sha256: str) -> AsyncIterator[bytes]:
        key = _key(sha256)
        try:
            # The exit is shielded: a client-mods download disconnected while a
            # jar's read is pending unwinds through it inside a cancelled scope,
            # and the client's release must still complete (issue #3234).
            async with shielded_exit(self._client_factory()) as client:
                # get_object already raises NotFoundError on a missing key, so no
                # redundant head_object first.
                async for chunk in await client.get_object(key):
                    yield chunk
        except NotFoundError as exc:
            # ``open`` does no I/O itself, so this fires on the first iteration.
            # The catalog resolver catches it and downloads instead (the cache is
            # an optimisation there, issue #2346); for the callers that open a
            # blob a plugin row still references it is a storage-consistency
            # fault. Translating keeps the storage type from crossing the seam.
            raise PluginCacheBlobNotFoundError(key) from exc
        except ObjectStoreUnavailableError as exc:
            # Not a miss: the blob may be there, so the resolver must not download
            # around it, and a mid-body failure must not pass for a short jar.
            raise PluginCacheStorageUnavailableError(key) from exc

    async def list_entries(self) -> list[CacheEntry]:
        prefix = "plugin-cache/"
        try:
            async with self._client_factory() as client:
                objs = await client.list_objects(prefix)
        except ObjectStoreUnavailableError as exc:
            raise PluginCacheStorageUnavailableError(prefix) from exc
        return [
            CacheEntry(
                sha256=obj.key.removeprefix(prefix),
                size_bytes=obj.size,
                modified_at=obj.last_modified,
            )
            for obj in objs
        ]

    async def delete(self, sha256: str) -> None:
        key = _key(sha256)
        try:
            async with self._client_factory() as client:
                await client.delete_object(key)
        except ObjectStoreUnavailableError as exc:
            raise PluginCacheStorageUnavailableError(key) from exc
