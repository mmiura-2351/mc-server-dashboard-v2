"""Object-store implementation of the ``ModStore`` Port.

Stores mod jar blobs under the ``mods/<mod-id>/<filename>`` key namespace
(top-level, outside ``communities/``). Uses the same
:class:`~...storage.adapters.object_store.S3ClientFactory` as the main
``ObjectStorage`` adapter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from mc_server_dashboard_api.servers.domain.mod import ModId
from mc_server_dashboard_api.servers.domain.mod_store import ModStore
from mc_server_dashboard_api.storage.adapters.object_store import S3ClientFactory
from mc_server_dashboard_api.storage.domain.errors import NotFoundError


def _key(mod_id: ModId, filename: str) -> str:
    return f"mods/{mod_id.value}/{filename}"


class ObjectModStore(ModStore):
    """:class:`ModStore` adapter over an S3-compatible object store."""

    def __init__(self, client_factory: S3ClientFactory) -> None:
        self._client_factory = client_factory

    async def put(
        self, mod_id: ModId, filename: str, stream: AsyncIterator[bytes]
    ) -> None:
        key = _key(mod_id, filename)
        async with self._client_factory() as client:
            await client.upload_multipart(key, stream)

    def open(self, mod_id: ModId, filename: str) -> AsyncIterator[bytes]:
        return self._open_gen(mod_id, filename)

    async def _open_gen(self, mod_id: ModId, filename: str) -> AsyncIterator[bytes]:
        key = _key(mod_id, filename)
        async with self._client_factory() as client:
            async for chunk in await client.get_object(key):
                yield chunk

    async def delete(self, mod_id: ModId) -> None:
        prefix = f"mods/{mod_id.value}/"
        async with self._client_factory() as client:
            objects = await client.list_objects(prefix)
            for obj in objects:
                await client.delete_object(obj.key)

    async def size(self, mod_id: ModId, filename: str) -> int:
        key = _key(mod_id, filename)
        async with self._client_factory() as client:
            result = await client.head_object(key)
            if result is None:
                raise NotFoundError(f"mod not found: {key}")
            return result
