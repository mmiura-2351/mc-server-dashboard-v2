"""The ``SetWorkerDrain`` use case: toggle a Worker's drain flag (FR-WRK-5).

Draining a Worker has two effects that ship together (FR-WRK-5): the Worker is
excluded from placement (the registry flag), AND every server currently assigned
to it is marked ``desired=stopped`` so the operator can take the host down with
its servers gracefully stopped and their final snapshot captured. This use case
only records the *intent*: it flips the registry flag and compare-and-sets each
assigned, desired-running server to ``desired=stopped`` (skipping any already
stopped). The actual stop + final snapshot is then driven by the reconciler's
existing ``redispatch_stop`` convergence — the HTTP call does not block on the
stops completing, and no new orchestration loop is introduced.

Un-draining (``draining=False``) only re-enables placement: it clears the flag
and does NOT resurrect ``desired=running`` on the servers drain stopped. Drain's
stops are explicit operator intents (a final snapshot was taken); restarting them
is a deliberate per-server start, not a side effect of clearing the flag.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.fleet.domain.registry import WorkerRegistry
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    DesiredState,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    WorkerId as ServersWorkerId,
)


@dataclass(frozen=True)
class SetWorkerDrain:
    registry: WorkerRegistry
    uow: UnitOfWork
    clock: Clock

    async def __call__(self, *, worker_id: WorkerId, draining: bool) -> int | None:
        """Toggle the drain flag and, on drain, mark assigned servers stopped.

        Returns ``None`` for an unknown Worker id (the caller maps it to 404).
        Otherwise returns the count of servers this call marked ``desired=stopped``
        — always ``0`` when ``draining`` is ``False`` (un-drain flips no server).
        """
        if not self.registry.set_draining(worker_id, draining):
            return None
        if not draining:
            return 0
        return await self._stop_assigned_servers(worker_id)

    async def _stop_assigned_servers(self, worker_id: WorkerId) -> int:
        """CAS every assigned desired-running server to ``desired=stopped``.

        The fleet worker id is a string; servers persist their assigned worker as a
        UUID (the control-plane seam bridges the two, #93). A Worker that never
        registered with a UUID-format id can hold no assigned servers, so a
        non-UUID id simply matches nothing here.
        """
        try:
            servers_worker_id = ServersWorkerId(uuid.UUID(worker_id.value))
        except ValueError:
            return 0
        stopped = 0
        async with self.uow:
            assigned = [
                server
                for server in await self.uow.servers.list_running_assigned()
                if server.assigned_worker_id == servers_worker_id
            ]
            for server in assigned:
                server.desired_state = DesiredState.STOPPED
                server.updated_at = self.clock.now()
                # Per-server compare-and-set: skip a server a concurrent stop already
                # moved out of running. The assignment is left intact — the
                # reconciler's redispatch_stop clears it on the confirmed stop.
                applied = await self.uow.servers.update_lifecycle(
                    server, expected_from=DesiredState.RUNNING
                )
                if applied:
                    stopped += 1
            await self.uow.commit()
        return stopped
