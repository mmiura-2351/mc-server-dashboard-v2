"""Storage Port for the content-addressed plugin/mod jar cache (issue #1306).

A blob store for the ``plugin-cache/`` namespace in object storage, keyed by the
SHA-256 content address of the jar bytes. The cache sits behind plugin ingest:
identical content is stored once and reused across servers, and a Modrinth
version downloaded once is served from the cache for later per-server installs.

The store is invisible to the user — the jar still materializes in each server's
working set via the :class:`FileStore` seam; this only deduplicates the at-rest
copy and short-circuits redundant downloads.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator

ByteStream = AsyncIterator[bytes]


class PluginCacheStore(abc.ABC):
    """Port: content-addressed blob storage for cached plugin/mod jars."""

    @abc.abstractmethod
    async def has(self, sha256: str) -> bool:
        """Return whether a blob with this content address is already cached."""

    @abc.abstractmethod
    async def put(self, sha256: str, stream: ByteStream) -> None:
        """Store a jar blob under its content address (idempotent dedup)."""

    @abc.abstractmethod
    def open(self, sha256: str) -> ByteStream:
        """Open a read stream over a cached jar. Raises if absent."""
