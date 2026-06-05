"""Bind the versions :class:`JarPool` seam to the storage :class:`JarStore`.

An adapter-layer composition across bounded contexts (mirroring the servers
control-plane adapter binding to the fleet): the versions *domain*/*application*
never import the storage context, so this adapter — bound only in the wiring
module — translates the storage ``JarStore`` slice into the narrow ``JarPool`` the
ensure-on-start use case depends on. The content key crosses the seam as a plain
SHA-256 string; the storage ``JarKey`` is constructed/unwrapped here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from mc_server_dashboard_api.storage.domain.port import JarStore
from mc_server_dashboard_api.storage.domain.value_objects import JarKey
from mc_server_dashboard_api.versions.domain.jar_pool import JarPool, PoolStats


@dataclass(frozen=True)
class StorageJarPool(JarPool):
    """Translate the storage ``JarStore`` slice into the versions ``JarPool``."""

    jars: JarStore

    async def has(self, sha256: str) -> bool:
        return await self.jars.has_jar(JarKey(sha256))

    async def put(self, data: bytes) -> str:
        async def _stream() -> AsyncIterator[bytes]:
            yield data

        key = await self.jars.put_jar(_stream())
        return key.sha256

    async def stats(self) -> PoolStats:
        s = await self.jars.jar_pool_stats()
        return PoolStats(count=s.count, total_bytes=s.total_bytes)
