"""Bind the :class:`LivePluginCacheReferences` seam to the plugin DB rows.

Reads every distinct non-null ``sha256`` from the ``server_plugin`` table --
the set of cached blobs that are still referenced by at least one installed
plugin. The plugin cache GC (issue #1332) diffs the cache contents against
this set to find orphaned blobs.

A bounded scan of all plugin rows. The GC runs periodically (daily default),
so loading through the servers ``UnitOfWork`` is acceptable -- same posture
as :class:`~...versions.adapters.server_jar_references.ServerJarReferences`.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.servers.application.plugin_cache_gc import (
    LivePluginCacheReferences,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class PluginCacheReferences(LivePluginCacheReferences):
    """Read live plugin-cache content keys from ``server_plugin`` rows."""

    uow: UnitOfWork

    async def live(self) -> set[str]:
        async with self.uow:
            return await self.uow.plugins.all_sha256s()
