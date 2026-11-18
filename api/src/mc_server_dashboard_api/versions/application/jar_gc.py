"""Reference-counted garbage collection of the content-addressed JAR pool (D4, #293).

Reclaim pooled JARs no longer referenced by any live server row.

**Reference model.** A pooled JAR (content-addressed by its SHA-256) is LIVE iff
some server row's config records it as its resolved JAR
(``resolved_jar_sha256``) — the bounded scan the :class:`LiveJarReferences` seam
returns. The model rests on three facts read from the code:

- The hydrate endpoint splices that exact pooled JAR into the working-set tar at
  transfer time (dataplane/api/transfers.py), keyed off the row's resolved key,
  so a server with a live row NEEDS its pooled JAR to start.
- Published snapshots and backups EMBED ``server.jar`` inside their own tars: the
  Worker's snapshot packs the whole working dir and excludes nothing at M1
  (worker datatransfer ``packTar``), and the JAR was unpacked there by a prior
  hydrate. So a restore needs no pool copy — snapshots/backups do NOT pin pool
  JARs.
- Deleting a server removes its rows, working set, and backups together, so it
  drops the only reference (the config key) pinning its JAR.

Everything else in the pool — a JAR present but referenced by no row — is an
orphan to reclaim.

**Safety window.** ``StartServer`` ensures (downloads + stores) the resolved JAR
into the pool BEFORE it commits the server row carrying that JAR's key
(servers/application/lifecycle.py: ``_ensure_jar`` runs first, the
``config[resolved_jar_sha256]`` write + ``commit`` come after placement). So
there is a window where a freshly-pooled JAR is present but not yet referenced by
any committed row — exactly an orphan to this GC. Deleting it would race an
in-flight start. We therefore never delete a JAR younger than
:data:`GC_SAFETY_WINDOW` (its store/upload time vs now); the window only has to
exceed the put-to-commit gap of a normal start, and one hour is comfortably
beyond it while keeping reclaim timely.

**Ownership.** The reference model holds only if the pool's store belongs to this
database: on a store another database also uses, that database's JARs are
referenced by no row here. Each pass therefore asks the store's owner marker first
and raises :class:`StoreNotOwnedError` unless it names this installation (STORAGE.md
Section 4.3, issue #3870).

**Record and failures.** Each reclaimed JAR is logged at INFO with its key, size and
age, and each pass ends with one summary line, at DEBUG when the pass neither
reclaimed nor failed anything. A JAR whose delete fails is logged at WARNING and
skipped, and the pass goes on: one JAR that keeps failing must not starve every JAR
after it (issue #3871). Five failed deletes in a row end the pass, and its summary
is then at WARNING with the reason (``gc_pass``, issue #3906). A store that gives
no answer at all (:class:`JarPoolUnreachableError`) ends the pass at once, as does a
cancellation; neither logs a summary.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from mc_server_dashboard_api.gc_pass import (
    MAX_CONSECUTIVE_FAILED_DELETES,
    STOPPED_AFTER_CONSECUTIVE_FAILURES,
)
from mc_server_dashboard_api.versions.domain.clock import Clock
from mc_server_dashboard_api.versions.domain.errors import (
    JarPoolUnreachableError,
    StoreNotOwnedError,
)
from mc_server_dashboard_api.versions.domain.jar_pool import JarPool
from mc_server_dashboard_api.versions.domain.jar_references import LiveJarReferences
from mc_server_dashboard_api.versions.domain.store_ownership import StoreOwnership

_LOG = logging.getLogger(__name__)

# Never delete a JAR younger than this. It must exceed the window in which
# StartServer has put a JAR into the pool but not yet committed the server row
# that references it (ensure-then-commit ordering, lifecycle.py); an unreferenced
# but still-young JAR may be that in-flight start, not a true orphan. One hour is
# comfortably beyond a normal start's put-to-commit gap.
GC_SAFETY_WINDOW = dt.timedelta(hours=1)


@dataclass(frozen=True)
class JarGcResult:
    """What a GC pass scanned and reclaimed (the manual endpoint's response, #293).

    ``scanned`` is the number of JARs listed, ``deleted`` the orphans reclaimed,
    ``freed_bytes`` their combined on-store size, and ``failed`` the orphans whose
    delete failed and were skipped (issue #3871).
    """

    scanned: int
    deleted: int
    freed_bytes: int
    failed: int


@dataclass(frozen=True)
class RunJarPoolGc:
    """Sweep the JAR pool: delete unreferenced, old-enough JARs (D4)."""

    pool: JarPool
    references: LiveJarReferences
    clock: Clock
    ownership: StoreOwnership

    async def __call__(self) -> JarGcResult:
        refusal = await self.ownership.refusal()
        if refusal is not None:
            raise StoreNotOwnedError(f"jar-pool GC refused: {refusal}")
        started = self.clock.now()
        entries = await self.pool.list_entries()
        live = await self.references.live()
        now = self.clock.now()
        cutoff = now - GC_SAFETY_WINDOW
        deleted = 0
        freed_bytes = 0
        failed = 0
        failed_in_a_row = 0
        stopped = False
        for entry in entries:
            if entry.sha256 in live:
                continue
            if entry.modified_at > cutoff:
                # Inside the safety window: may be an in-flight start's just-pooled
                # JAR whose server row has not committed yet. Spare it.
                continue
            # Re-check live references immediately before delete (issue #2541):
            # on the object backend a re-put of an already-pooled JAR skips the
            # upload, so ``ensure_jar`` re-pooling a JAR does not refresh its
            # store time and the safety window alone cannot protect it. Between
            # the snapshot above and this point, a start may have committed the
            # row referencing it — re-checking avoids reclaiming a live JAR. It
            # stays outside the ``try`` below: a re-read that fails is not a
            # failed delete, and it ends the pass (issue #3906).
            fresh_live = await self.references.live()
            if entry.sha256 in fresh_live:
                continue
            try:
                await self.pool.delete(entry.sha256)
            except JarPoolUnreachableError:
                # Not this JAR but the store: every later delete would fail the
                # same way, so the pass ends here.
                raise
            except Exception as exc:  # noqa: BLE001 - one JAR must not end the pass
                failed += 1
                failed_in_a_row += 1
                _LOG.warning(
                    "jar-pool GC could not delete jar %s, skipped it: %s: %s",
                    entry.sha256,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                if failed_in_a_row >= MAX_CONSECUTIVE_FAILED_DELETES:
                    # Likely the store rather than these JARs: stop paying each
                    # later one its retries and a WARNING (issue #3906).
                    stopped = True
                    break
                continue
            failed_in_a_row = 0
            deleted += 1
            freed_bytes += entry.size_bytes
            _LOG.info(
                "jar-pool GC reclaimed jar %s (%d bytes, stored %s ago): no server "
                "references it",
                entry.sha256,
                entry.size_bytes,
                now - entry.modified_at,
            )
        level = logging.INFO if deleted or failed else logging.DEBUG
        _LOG.log(
            logging.WARNING if stopped else level,
            "jar-pool GC pass: %d jar(s) listed, %d reclaimed (%d bytes), %d failed, "
            "in %.1fs",
            len(entries),
            deleted,
            freed_bytes,
            failed,
            (self.clock.now() - started).total_seconds(),
            extra={"reason": STOPPED_AFTER_CONSECUTIVE_FAILURES} if stopped else None,
        )
        return JarGcResult(
            scanned=len(entries),
            deleted=deleted,
            freed_bytes=freed_bytes,
            failed=failed,
        )
