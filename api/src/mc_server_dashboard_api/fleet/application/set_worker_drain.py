"""The ``SetWorkerDrain`` use case: toggle a Worker's drain flag (FR-WRK-5).

Draining a Worker has two effects that ship together (FR-WRK-5): the Worker is
excluded from placement (the registry flag), AND every server currently assigned
to it is marked ``desired=stopped`` so the operator can take the host down with
its servers gracefully stopped and their final snapshot captured. This use case
only records the *intent*: it flips the registry flag and compare-and-sets each
assigned, desired-running server to ``desired=stopped`` (skipping any already
stopped). The actual stop is then driven by the reconciler's existing
``redispatch_stop`` convergence, which since #849 also takes the post-stop final
snapshot (the stop scratch is held for it since #845) — the HTTP call does not
block on the stops completing, and no new orchestration loop is introduced.

Convergence is ASYNCHRONOUS and needs the Worker connected: the actual stop +
snapshot only happen after the reconciler's grace window and only while the
Worker stays heartbeating (the reconciler skips disconnected Workers). An
operator following the FR-WRK-5 workflow MUST keep the Worker up until the stops
converge — shutting the host down immediately defers every stop (and its
snapshot) until the Worker reconnects. The returned count is the number of
servers this call *marked*, not the number already stopped; watch ``GET
/workers`` assigned load (or the per-server states) drop to confirm convergence.

A start racing the drain can leak: if a start was already in flight when drain
ran (placement chose this Worker before the flag flipped), it can commit
``desired=running`` + assignment after :meth:`_stop_assigned_servers` listed its
targets, leaving a server running on the draining Worker that the reconciler
never acts on (desired matches observed). Re-issuing the PUT (idempotent
re-drain) catches it.

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

        Each applied CAS decrements the Worker's placement load, mirroring
        ``StopServer.__call__`` (lifecycle.py): drain owns the
        ``desired=running -> stopped`` flip here, so it owns the decrement that
        pairs with it — the in-memory load is "servers assigned with
        desired=running" (the tally rebuilt on reconnect via ``set_assignment``),
        so flipping desired=stopped without decrementing leaves the load inflated
        until the next reconnect. The reconciler's ``redispatch_stop`` deliberately
        does NOT decrement again (it assumes the original stop already did), so
        this is the single decrement for the drain path. ``decrement_assignment``
        floors at zero, so a same-instant reconnect rebuild that already excluded
        this (now desired=stopped) server makes the decrement a harmless no-op
        rather than a double-count — the same race StopServer's decrement carries.
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
                    self.registry.decrement_assignment(worker_id)
            await self.uow.commit()
        return stopped
