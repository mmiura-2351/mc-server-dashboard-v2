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
snapshot only happen after the reconciler's grace window (``grace_seconds``,
120s default) plus a reconciler tick, and only while the Worker stays
heartbeating (the reconciler skips disconnected Workers). An operator following
the FR-WRK-5 workflow MUST keep the Worker up until the stops converge —
shutting the host down immediately defers every stop (and its final snapshot)
until the Worker reconnects, which in a decommission never happens. The returned
count is the number of servers this call *marked*, not the number already
stopped. Confirm convergence PER SERVER, not by assigned load: this call's
placement-load decrement (see :meth:`_stop_assigned_servers`) drops ``GET
/workers`` assigned load to 0 synchronously, before any stop runs, so load is not
a convergence signal. Instead watch each drain-marked server reach
``observed=stopped`` and unassigned (the admin servers list / per-server detail).

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
Un-draining BEFORE the drained set has converged opens a transient
oversubscription window: drain freed the placement load at flip time, so a
re-enabled Worker can take new placements while its drained instances are still
winding down (until ``redispatch_stop`` converges, ~grace + a tick per server).
This matches the normal stop path's "load = assigned with desired=running"
window (seconds there, minutes here); wait for convergence before un-draining to
avoid it.
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
        until the next reconnect. The decrements run AFTER ``commit`` returns
        (like ``StopServer.__call__``): a failed commit rolls back the CAS flips,
        so decrementing only the committed flips keeps the in-memory load in step
        with the persisted desired states rather than leaking decrements on a
        rollback. The reconciler's ``redispatch_stop`` deliberately does NOT
        decrement again (it assumes the original stop already did), so this is the
        single decrement for the drain path. ``decrement_assignment`` drops the
        per-server committed row; it is a harmless no-op when a same-instant
        reconnect rebuild already excluded this server (idempotent pop), the same
        self-healing race class StopServer's decrement carries.
        """
        try:
            servers_worker_id = ServersWorkerId(uuid.UUID(worker_id.value))
        except ValueError:
            return 0
        stopped_ids: list[str] = []
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
                    stopped_ids.append(str(server.id.value))
            await self.uow.commit()
        # Decrement only the committed flips, after the commit lands: a failed
        # commit rolls back the CAS flips above, so the decrements must not run
        # before it (mirroring StopServer.__call__'s post-commit decrement). Pass the
        # per-server id so the registry sheds each assignment's committed memory with
        # its count (#843).
        for server_id in stopped_ids:
            self.registry.decrement_assignment(worker_id, server_id)
        return len(stopped_ids)
