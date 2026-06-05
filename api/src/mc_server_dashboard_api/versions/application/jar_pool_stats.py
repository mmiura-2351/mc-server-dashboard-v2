"""Read use case for the JAR-pool stats endpoint (issue #286).

A thin wrapper over the :class:`JarPool` seam: count + total bytes of the pooled
JARs, for a platform admin's operational visibility. No GC and no reference count
(that is #32 / D4) — stats only.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.versions.domain.jar_pool import JarPool, PoolStats


@dataclass(frozen=True)
class GetJarPoolStats:
    """Return the pooled-JAR count and total bytes (JAR-pool read)."""

    pool: JarPool

    async def __call__(self) -> PoolStats:
        return await self.pool.stats()
