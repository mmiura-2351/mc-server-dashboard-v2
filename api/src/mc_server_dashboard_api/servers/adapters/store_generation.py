"""Storage-backed adapter for the servers :class:`StoreGenerationReader` seam.

Binds the reconciler's skip-hydrate threshold (issue #763) to Storage's
authoritative ``current_generation`` — the single source of truth that advances
atomically with the published working set — instead of the lag-prone
``server.store_generation`` DB mirror. It maps the servers value objects onto the
storage value objects at the bounded-context boundary (the lifecycle layer never
imports the storage domain).
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.store_generation import (
    StoreGenerationReader,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)
from mc_server_dashboard_api.storage.domain.port import Storage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)


@dataclass(frozen=True)
class StorageGenerationReader(StoreGenerationReader):
    """Read Storage's authoritative working-set generation (issue #763)."""

    storage: Storage

    async def current_generation(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> int:
        return await self.storage.current_generation(
            StorageCommunityId(community_id.value),
            StorageServerId(server_id.value),
        )
