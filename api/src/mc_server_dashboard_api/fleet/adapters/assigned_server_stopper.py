"""Bind the fleet ``AssignedServerStopper`` Port to the servers stop procedure.

An adapter-layer composition across bounded contexts (the same shape as the
versions ``LiveJarReferences`` -> servers binding): the fleet *domain* and
*application* never import the servers context — this adapter, bound only in the
wiring, calls the servers ``StopServersAssignedToWorker`` use case, which owns the
per-server compare-and-set and the transaction (issue #2578).

The identifier bridge lives here because it is exactly the boundary crossing: the
fleet worker id is an opaque string (CONFIGURATION.md Section 6.1 ``worker.id``)
while servers persist their assigned worker as a UUID (the control-plane seam
bridges the two, #93). A Worker that never registered with a UUID-format id can
hold no assigned servers, so a non-UUID id matches nothing rather than failing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.fleet.domain.assigned_server_stopper import (
    AssignedServerStopper,
)
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from mc_server_dashboard_api.servers.application.lifecycle import (
    StopServersAssignedToWorker,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    WorkerId as ServersWorkerId,
)


@dataclass(frozen=True)
class ServersAssignedServerStopper(AssignedServerStopper):
    """Stop a Worker's assigned servers through the servers lifecycle use case."""

    stop_servers: StopServersAssignedToWorker

    async def stop_assigned(self, worker_id: WorkerId) -> list[str]:
        try:
            servers_worker_id = ServersWorkerId(uuid.UUID(worker_id.value))
        except ValueError:
            return []
        stopped = await self.stop_servers(worker_id=servers_worker_id)
        return [str(server_id.value) for server_id in stopped]
