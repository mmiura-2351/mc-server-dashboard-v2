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
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from mc_server_dashboard_api.versions.domain.clock import Clock
from mc_server_dashboard_api.versions.domain.jar_pool import JarPool
from mc_server_dashboard_api.versions.domain.jar_references import LiveJarReferences

# Never delete a JAR younger than this. It must exceed the window in which
# StartServer has put a JAR into the pool but not yet committed the server row
# that references it (ensure-then-commit ordering, lifecycle.py); an unreferenced
# but still-young JAR may be that in-flight start, not a true orphan. One hour is
# comfortably beyond a normal start's put-to-commit gap.
GC_SAFETY_WINDOW = dt.timedelta(hours=1)


@dataclass(frozen=True)
class JarGcResult:
    """What a GC pass scanned and reclaimed (the manual endpoint's response, #293).

    ``scanned`` is the pool size examined, ``deleted`` the orphans reclaimed, and
    ``freed_bytes`` their combined on-store size.
    """

    scanned: int
    deleted: int
    freed_bytes: int


@dataclass(frozen=True)
class RunJarPoolGc:
    """Sweep the JAR pool: delete unreferenced, old-enough JARs (D4)."""

    pool: JarPool
    references: LiveJarReferences
    clock: Clock

    async def __call__(self) -> JarGcResult:
        entries = await self.pool.list_entries()
        live = await self.references.live()
        cutoff = self.clock.now() - GC_SAFETY_WINDOW
        deleted = 0
        freed_bytes = 0
        for entry in entries:
            if entry.sha256 in live:
                continue
            if entry.modified_at > cutoff:
                # Inside the safety window: may be an in-flight start's just-pooled
                # JAR whose server row has not committed yet. Spare it.
                continue
            await self.pool.delete(entry.sha256)
            deleted += 1
            freed_bytes += entry.size_bytes
        return JarGcResult(
            scanned=len(entries), deleted=deleted, freed_bytes=freed_bytes
        )
