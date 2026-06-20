"""Object-store implementation of the ``ResourcePackStore`` Port.

Stores resource pack blobs under the ``resource-packs/<pack-id>/<filename>``
key namespace (top-level, outside ``communities/``). Uses the same
:class:`~...storage.adapters.object_store.S3ClientFactory` as the main
``ObjectStorage`` adapter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from mc_server_dashboard_api.servers.domain.resource_pack import ResourcePackId
from mc_server_dashboard_api.servers.domain.resource_pack_store import (
    ResourcePackStore,
)
from mc_server_dashboard_api.storage.adapters.object_store import S3ClientFactory
from mc_server_dashboard_api.storage.domain.errors import NotFoundError


def _key(pack_id: ResourcePackId, filename: str) -> str:
    return f"resource-packs/{pack_id.value}/{filename}"


class ObjectResourcePackStore(ResourcePackStore):
    """:class:`ResourcePackStore` adapter over an S3-compatible object store."""

    def __init__(self, client_factory: S3ClientFactory) -> None:
        self._client_factory = client_factory

    async def put(
        self, pack_id: ResourcePackId, filename: str, stream: AsyncIterator[bytes]
    ) -> None:
        key = _key(pack_id, filename)
        async with self._client_factory() as client:
            await client.upload_multipart(key, stream)

    def open(self, pack_id: ResourcePackId, filename: str) -> AsyncIterator[bytes]:
        return self._open_gen(pack_id, filename)

    async def _open_gen(
        self, pack_id: ResourcePackId, filename: str
    ) -> AsyncIterator[bytes]:
        key = _key(pack_id, filename)
        async with self._client_factory() as client:
            async for chunk in await client.get_object(key):
                yield chunk

    async def delete(self, pack_id: ResourcePackId) -> None:
        prefix = f"resource-packs/{pack_id.value}/"
        async with self._client_factory() as client:
            objects = await client.list_objects(prefix)
            for obj in objects:
                await client.delete_object(obj.key)

    async def size(self, pack_id: ResourcePackId, filename: str) -> int:
        key = _key(pack_id, filename)
        async with self._client_factory() as client:
            result = await client.head_object(key)
            if result is None:
                raise NotFoundError(f"resource pack not found: {key}")
            return result
