"""Release a held server without its final snapshot (issue #4055)."""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.servers.application.stop_convergence import (
    StopConvergenceGuard,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)


@dataclass
class ReleaseHeldServer:
    uow: UnitOfWork
    stop_convergence: StopConvergenceGuard

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> Server:
        raise NotImplementedError
