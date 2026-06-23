"""Content-addressed cache ingest helper (issue #1306).

Plugin/mod install use cases ingest the jar bytes into the content-addressed
cache before (or instead of) deploying them to the server's working set. Ingest
computes the SHA-256 content address and stores the blob once — identical
content across servers reuses the cached blob (dedup-on-ingest, in the store).

This is invisible to the user: the jar still materializes in each server's
working set through the :class:`FileStore` seam. The cache only deduplicates the
at-rest copy and feeds the Modrinth download cache.
"""

from __future__ import annotations

import hashlib

from mc_server_dashboard_api.servers.domain.plugin_cache_store import (
    ByteStream,
    PluginCacheStore,
)


async def _bytes_stream(data: bytes) -> ByteStream:
    """Wrap ``bytes`` into an ``AsyncIterator[bytes]``."""

    yield data


async def ingest_into_cache(cache: PluginCacheStore, content: bytes) -> str:
    """Store jar ``content`` in the cache and return its SHA-256 content address.

    The store dedups on its content key, so a second install of identical bytes
    skips the upload and reuses the cached blob.
    """

    sha256 = hashlib.sha256(content).hexdigest()
    await cache.put(sha256, _bytes_stream(content))
    return sha256
