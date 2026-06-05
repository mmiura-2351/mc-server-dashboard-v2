"""Read-only port-availability use cases (issue #243).

Back the ``GET /ports/check/{port}`` and ``GET /ports/available`` endpoints. Both
read the deployment-wide taken-port set from the repository and apply the pure
allocation policy (:mod:`servers.domain.ports`). Game ports are a deployment
resource, so these reads are not community-scoped -- any authenticated user may
query them.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.ports import PortRange, next_free_ports
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class CheckPort:
    """Report whether ``port`` is in the configured range and currently free."""

    uow: UnitOfWork
    port_range: PortRange

    async def __call__(self, *, port: int) -> dict[str, object]:
        async with self.uow:
            taken = await self.uow.servers.list_game_ports()
        in_range = port in self.port_range
        # A port is assignable only if it is in range AND not already taken; an
        # out-of-range port is reported unavailable regardless of the taken set.
        available = in_range and port not in taken
        return {"port": port, "in_range": in_range, "available": available}


@dataclass(frozen=True)
class ListAvailablePorts:
    """Return up to ``count`` lowest free in-range ports (ascending)."""

    uow: UnitOfWork
    port_range: PortRange

    async def __call__(self, *, count: int) -> list[int]:
        async with self.uow:
            taken = await self.uow.servers.list_game_ports()
        return next_free_ports(self.port_range, taken=taken, count=count)
