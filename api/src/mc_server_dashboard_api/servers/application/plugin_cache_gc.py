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
blob after it (issue #3871). Five failed deletes in a row end the pass, and its
summary is then at WARNING with the reason (``gc_pass``, issue #3906). A store that
gives no answer at all (:class:`PluginCacheStorageUnreachableError`) ends the pass
at once, as does a cancellation; neither logs a summary. A pass that ends early, on
a delete that got no answer or at the fifth failed delete in a row, leaves the blob
it ended at in the GC's :class:`ResumePoint`. The next pass walks the blobs in key
order starting after it, so failures tied in fact to those blobs cannot starve the
blobs after them (issue #3934). A pass that walks every blob without ending early
clears it, and a pass without one starts at a random place in key order.
"""

from __future__ import annotations

import abc
import datetime as dt
import logging
from dataclasses import dataclass

from mc_server_dashboard_api.gc_pass import (
    MAX_CONSECUTIVE_FAILED_DELETES,
    STOPPED_AFTER_CONSECUTIVE_FAILURES,
    ResumePoint,
)
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

    ``scanned`` is the number of blobs listed, ``deleted`` the orphans reclaimed,
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
    resume_point: ResumePoint

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
        failed_in_a_row = 0
        stopped = False
        if self.resume_point.key is not None:
            _LOG.debug(
                "plugin-cache GC pass starts after blob %s, where the last pass "
                "ended early",
                self.resume_point.key,
            )
        for entry in self.resume_point.order(entries, lambda e: e.sha256):
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
            # have committed — re-checking avoids reclaiming a live blob. It stays
            # outside the ``try`` below: a re-read that fails is not a failed
            # delete, and it ends the pass (issue #3906).
            fresh_live = await self.references.live()
            if entry.sha256 in fresh_live:
                continue
            try:
                await self.cache.delete(entry.sha256)
            except PluginCacheStorageUnreachableError:
                # Not this blob but the store: every later delete would fail the
                # same way, so the pass ends here. If it is this blob after all,
                # the next pass starts after it (issue #3934).
                self.resume_point.key = entry.sha256
                raise
            except Exception as exc:  # noqa: BLE001 - one blob must not end the pass
                failed += 1
                failed_in_a_row += 1
                _LOG.warning(
                    "plugin-cache GC could not delete blob %s, skipped it: %s: %s",
                    entry.sha256,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                if failed_in_a_row >= MAX_CONSECUTIVE_FAILED_DELETES:
                    # Likely the store rather than these blobs: stop paying each
                    # later one its retries and a WARNING (issue #3906). The next
                    # pass starts after this one, in case they are these blobs
                    # after all (issue #3934).
                    self.resume_point.key = entry.sha256
                    stopped = True
                    break
                continue
            failed_in_a_row = 0
            deleted += 1
            freed_bytes += entry.size_bytes
            _LOG.info(
                "plugin-cache GC reclaimed blob %s (%d bytes, stored %s ago): no "
                "plugin row names it",
                entry.sha256,
                entry.size_bytes,
                now - entry.modified_at,
            )
        if not stopped:
            # The pass came round without ending early (issue #3934).
            self.resume_point.key = None
        level = logging.INFO if deleted or failed else logging.DEBUG
        _LOG.log(
            logging.WARNING if stopped else level,
            "plugin-cache GC pass: %d blob(s) listed, %d reclaimed (%d bytes), "
            "%d failed, in %.1fs",
            len(entries),
            deleted,
            freed_bytes,
            failed,
            (self.clock.now() - started).total_seconds(),
            extra={"reason": STOPPED_AFTER_CONSECUTIVE_FAILURES} if stopped else None,
        )
        return PluginCacheGcResult(
            scanned=len(entries),
            deleted=deleted,
            freed_bytes=freed_bytes,
            failed=failed,
        )
