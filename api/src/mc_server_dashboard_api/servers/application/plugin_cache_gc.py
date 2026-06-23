"""Garbage collection of the content-addressed plugin cache (issue #1332).

Reclaim cached plugin/mod jars no longer referenced by any ``server_plugin``
row. Mirrors the JAR-pool GC (D4, issue #293) in structure and safety model.

**Reference model.** A cached blob (keyed by SHA-256) is LIVE iff some
``server_plugin`` row records it as its ``sha256`` field. Multiple servers may
share the same cached blob (content-addressed dedup), so a blob is only orphaned
when *no* row references it.

**Safety window.** ``InstallFromCatalog`` caches the jar blob *before*
committing the ``server_plugin`` row. There is a window where a freshly-cached
blob is present but not yet referenced by any committed row -- exactly an orphan
to this GC. Deleting it would race an in-flight install. We therefore never
delete a blob younger than :data:`GC_SAFETY_WINDOW`; one hour is comfortably
beyond the cache-to-commit gap of a normal install.
"""

from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.plugin_cache_store import (
    PluginCacheStore,
)

# Never delete a blob younger than this. See module docstring.
GC_SAFETY_WINDOW = dt.timedelta(hours=1)


@dataclass(frozen=True)
class PluginCacheGcResult:
    """What a GC pass scanned and reclaimed.

    ``scanned`` is the cache size examined, ``deleted`` the orphans reclaimed,
    and ``freed_bytes`` their combined on-store size.
    """

    scanned: int
    deleted: int
    freed_bytes: int


class LivePluginCacheReferences(abc.ABC):
    """Port: the set of cached-blob content keys live plugin rows reference."""

    @abc.abstractmethod
    async def live(self) -> set[str]:
        """Return every distinct ``sha256`` referenced by a ``server_plugin`` row."""


@dataclass(frozen=True)
class RunPluginCacheGc:
    """Sweep the plugin cache: delete unreferenced, old-enough blobs."""

    cache: PluginCacheStore
    references: LivePluginCacheReferences
    clock: Clock

    async def __call__(self) -> PluginCacheGcResult:
        entries = await self.cache.list_entries()
        live = await self.references.live()
        cutoff = self.clock.now() - GC_SAFETY_WINDOW
        deleted = 0
        freed_bytes = 0
        for entry in entries:
            if entry.sha256 in live:
                continue
            if entry.modified_at > cutoff:
                # Inside the safety window: may be an in-flight install's
                # just-cached blob whose plugin row has not committed yet.
                continue
            # Re-check live references immediately before delete (issue #1404):
            # a dedup install reuses an existing blob without refreshing its
            # modified_at, so the safety window alone cannot protect it.
            # Between the snapshot above and this point, a new plugin row may
            # have committed — re-checking avoids reclaiming a live blob.
            fresh_live = await self.references.live()
            if entry.sha256 in fresh_live:
                continue
            await self.cache.delete(entry.sha256)
            deleted += 1
            freed_bytes += entry.size_bytes
        return PluginCacheGcResult(
            scanned=len(entries), deleted=deleted, freed_bytes=freed_bytes
        )
