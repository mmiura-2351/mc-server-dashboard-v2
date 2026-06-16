"""Storage Port for resource pack blob data.

A simple blob store for the ``resource-packs/`` namespace in object storage.
Resource packs are global (not community-scoped), keyed by
``resource-packs/<pack-id>/<filename>``.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator

from mc_server_dashboard_api.servers.domain.resource_pack import ResourcePackId

ByteStream = AsyncIterator[bytes]


class ResourcePackStore(abc.ABC):
    """Port: blob storage for resource pack files."""

    @abc.abstractmethod
    async def put(
        self, pack_id: ResourcePackId, filename: str, stream: ByteStream
    ) -> None:
        """Store a resource pack blob."""

    @abc.abstractmethod
    def open(self, pack_id: ResourcePackId, filename: str) -> ByteStream:
        """Open a read stream over a stored resource pack."""

    @abc.abstractmethod
    async def delete(self, pack_id: ResourcePackId) -> None:
        """Delete a resource pack's blob data."""

    @abc.abstractmethod
    async def size(self, pack_id: ResourcePackId, filename: str) -> int:
        """Return the size in bytes of a stored resource pack."""
