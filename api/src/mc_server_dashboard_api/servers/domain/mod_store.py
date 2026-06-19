"""Storage Port for mod jar blob data.

A simple blob store for the ``mods/`` namespace in object storage. Mods are
global (not community-scoped), keyed by ``mods/<mod-id>/<filename>``.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator

from mc_server_dashboard_api.servers.domain.mod import ModId

ByteStream = AsyncIterator[bytes]


class ModStore(abc.ABC):
    """Port: blob storage for mod jar files."""

    @abc.abstractmethod
    async def put(self, mod_id: ModId, filename: str, stream: ByteStream) -> None:
        """Store a mod jar blob."""

    @abc.abstractmethod
    def open(self, mod_id: ModId, filename: str) -> ByteStream:
        """Open a read stream over a stored mod jar."""

    @abc.abstractmethod
    async def delete(self, mod_id: ModId) -> None:
        """Delete a mod's blob data."""

    @abc.abstractmethod
    async def size(self, mod_id: ModId, filename: str) -> int:
        """Return the size in bytes of a stored mod jar."""
