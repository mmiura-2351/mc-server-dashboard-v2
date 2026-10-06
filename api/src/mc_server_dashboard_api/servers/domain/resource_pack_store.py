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
        """Store a resource pack blob.

        Raises ``ResourcePackStorageUnavailableError`` when the store could not
        take the upload (issue #2458). Nothing is stored in that case, so the
        caller may simply try again.
        """

    @abc.abstractmethod
    def open(self, pack_id: ResourcePackId, filename: str) -> ByteStream:
        """Open a read stream over a stored resource pack.

        Performs no I/O itself: a missing blob raises ``ResourcePackNotFoundError``
        on the first iteration, not from this call, and a store outage raises
        ``ResourcePackStorageUnavailableError`` there (issue #2455). The download
        routes take that first iteration before they write their headers, so both
        still decide the status.
        """

    @abc.abstractmethod
    async def delete(self, pack_id: ResourcePackId) -> None:
        """Delete a resource pack's blob data.

        Idempotent: a pack with nothing stored is a no-op. Raises
        ``ResourcePackStorageUnavailableError`` when the store could not finish
        (issue #2458), possibly after removing part of the data; calling again
        removes the rest.
        """

    @abc.abstractmethod
    async def size(self, pack_id: ResourcePackId, filename: str) -> int:
        """Return the size in bytes of a stored resource pack.

        Raises ``ResourcePackNotFoundError`` when no blob is stored (issue #2321),
        and ``ResourcePackStorageUnavailableError`` when the store could not answer
        (issue #2455) — the same two outcomes :meth:`open` reports, so one outage
        yields one status whichever of the two a download strikes.
        """
