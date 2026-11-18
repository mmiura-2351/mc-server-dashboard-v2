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

**Ownership.** The reference model holds only if the bucket belongs to this
database: in a bucket another database also uses, that database's blobs are named
by no row here. Each pass therefore asks the bucket's owner marker first and is
refused, with an ERROR, unless it names this installation (STORAGE.md Section 4.3,
issue #3870).

**Record and failures.** Each reclaimed blob is logged at INFO with its key, size
and age, and each pass ends with one summary line, at DEBUG when the pass neither
reclaimed nor failed anything. A blob whose delete fails is logged at WARNING and
skipped, and the pass goes on: one blob that keeps failing must not starve every
blob after it (issue #3871). A store that gives no answer at all
(:class:`PluginCacheStorageUnreachableError`) ends the pass instead, as does a
cancellation.
"""

from __future__ import annotations

import abc
import datetime as dt
import logging
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.errors import (
    PluginCacheStorageUnreachableError,
)
from mc_server_dashboard_api.servers.domain.plugin_cache_store import (
    PluginCacheStore,
)
from mc_server_dashboard_api.servers.domain.store_ownership import StoreOwnership

_LOG = logging.getLogger(__name__)

# Never delete a blob younger than this. See module docstring.
GC_SAFETY_WINDOW = dt.timedelta(hours=1)


@dataclass(frozen=True)
class PluginCacheGcResult:
    """What a GC pass scanned and reclaimed.

    ``scanned`` is the cache size examined, ``deleted`` the orphans reclaimed,
    ``freed_bytes`` their combined on-store size, and ``failed`` the orphans whose
    delete failed and were skipped (issue #3871).
    """

    scanned: int
    deleted: int
    freed_bytes: int
    failed: int


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
    ownership: StoreOwnership

    async def __call__(self) -> PluginCacheGcResult:
        refusal = await self.ownership.refusal()
        if refusal is not None:
            _LOG.error("plugin-cache GC refused: %s", refusal)
            return PluginCacheGcResult(scanned=0, deleted=0, freed_bytes=0, failed=0)
        started = self.clock.now()
        entries = await self.cache.list_entries()
        live = await self.references.live()
        now = self.clock.now()
        cutoff = now - GC_SAFETY_WINDOW
        deleted = 0
        freed_bytes = 0
        failed = 0
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
            try:
                await self.cache.delete(entry.sha256)
            except PluginCacheStorageUnreachableError:
                # Not this blob but the store: every later delete would fail the
                # same way, so the pass ends here.
                raise
            except Exception as exc:  # noqa: BLE001 - one blob must not end the pass
                failed += 1
                _LOG.warning(
                    "plugin-cache GC could not delete blob %s, skipped it: %s",
                    entry.sha256,
                    exc,
                    exc_info=True,
                )
                continue
            deleted += 1
            freed_bytes += entry.size_bytes
            _LOG.info(
                "plugin-cache GC reclaimed blob %s (%d bytes, stored %s ago): no "
                "plugin row names it",
                entry.sha256,
                entry.size_bytes,
                now - entry.modified_at,
            )
        _LOG.log(
            logging.INFO if deleted or failed else logging.DEBUG,
            "plugin-cache GC pass: %d blob(s) listed, %d reclaimed (%d bytes), "
            "%d failed, in %.1fs",
            len(entries),
            deleted,
            freed_bytes,
            failed,
            (self.clock.now() - started).total_seconds(),
        )
        return PluginCacheGcResult(
            scanned=len(entries),
            deleted=deleted,
            freed_bytes=freed_bytes,
            failed=failed,
        )
