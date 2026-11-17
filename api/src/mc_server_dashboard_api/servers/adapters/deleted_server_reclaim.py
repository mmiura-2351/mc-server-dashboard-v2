"""Fleet-backed adapter for the servers :class:`DeletedServerReclaim` Port (#3679)."""

from __future__ import annotations

from mc_server_dashboard_api.fleet.adapters.control_plane import GrpcControlPlane
from mc_server_dashboard_api.fleet.domain.control_plane import (
    ReclaimDeletedServerCommand,
    WorkerNotConnectedError,
)
from mc_server_dashboard_api.fleet.domain.registry import WorkerRegistry
from mc_server_dashboard_api.servers.domain.deleted_server_reclaim import (
    DeletedServerReclaim,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


class FleetDeletedServerReclaim(DeletedServerReclaim):
    """Send ``ReclaimDeletedServer`` to every Worker with a live session.

    Every connected Worker is told, not only the ones the registry records as
    holding the server: a deleted server is unassigned, and a Worker can hold its
    scratch without the API having recorded it (a hydrate whose dispatch timed out
    still completes). A Worker that holds nothing for the id reclaims nothing.
    """

    def __init__(
        self, *, registry: WorkerRegistry, control_plane: GrpcControlPlane
    ) -> None:
        self._registry = registry
        self._control_plane = control_plane

    async def reclaim(self, server_id: ServerId) -> None:
        for worker in self._registry.list_workers():
            try:
                await self._control_plane.dispatch_fire_and_forget(
                    worker_id=worker.id,
                    server_id=str(server_id.value),
                    command=ReclaimDeletedServerCommand(),
                )
            except WorkerNotConnectedError:
                # Its next registration reports the id as deleted.
                continue
