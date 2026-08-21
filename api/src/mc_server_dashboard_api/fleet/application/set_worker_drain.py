"""The ``SetWorkerDrain`` use case: toggle a Worker's drain flag (FR-WRK-5).

Draining a Worker has two effects that ship together (FR-WRK-5): the Worker is
excluded from placement (the registry flag), AND every server currently assigned
to it is marked ``desired=stopped`` so the operator can take the host down with
its servers gracefully stopped and their final snapshot captured. This use case
only records the *intent*: through the ``AssignedServerStopper`` Port it
compare-and-sets each assigned, desired-running server to ``desired=stopped``
(skipping any already stopped), then flips the registry flag. The actual stop is
then driven by the reconciler's existing ``redispatch_stop`` convergence, which
since #849 also takes the post-stop final snapshot (the stop scratch is held for
it since #845) — the HTTP call does not block on the stops completing, and no new
orchestration loop is introduced.

Convergence is ASYNCHRONOUS and needs the Worker connected: the actual stop +
snapshot only happen after the reconciler's grace window (``grace_seconds``,
660s default) plus a reconciler tick, and only while the Worker stays
heartbeating (the reconciler skips disconnected Workers). An operator following
the FR-WRK-5 workflow MUST keep the Worker up until the stops converge —
shutting the host down immediately defers every stop (and its final snapshot)
until the Worker reconnects, which in a decommission never happens. The returned
count is the number of servers this call *marked*, not the number already
stopped. Confirm convergence PER SERVER, not by assigned load: this call's
placement-load decrement (see :meth:`_shed_placement_load`) drops ``GET
/workers`` assigned load to 0 synchronously, before any stop runs, so load is not
a convergence signal. Instead watch each drain-marked server reach
``observed=stopped`` and unassigned (the admin servers list / per-server detail).

A start racing the drain can leak: a start whose placement chose this Worker
before the flag flipped can commit ``desired=running`` + assignment after the stop
pass listed its targets, leaving a server running on the draining Worker that the
reconciler never acts on (desired matches observed). Since the flag flips only
AFTER those stops commit (see :meth:`__call__`), that window spans the stop
transaction as well as the placements already in flight when the call arrived —
the deliberate cost of the two sides never disagreeing on a failed drain.
Re-issuing the PUT (idempotent re-drain) catches it either way.

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

from dataclasses import dataclass

from mc_server_dashboard_api.fleet.domain.assigned_server_stopper import (
    AssignedServerStopper,
)
from mc_server_dashboard_api.fleet.domain.registry import WorkerRegistry
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId


@dataclass(frozen=True)
class SetWorkerDrain:
    registry: WorkerRegistry
    stopper: AssignedServerStopper

    async def __call__(self, *, worker_id: WorkerId, draining: bool) -> int | None:
        """Toggle the drain flag and, on drain, mark assigned servers stopped.

        Returns ``None`` for an unknown Worker id (the caller maps it to 404).
        Otherwise returns the count of servers this call marked ``desired=stopped``
        — always ``0`` when ``draining`` is ``False`` (un-drain flips no server).

        Ordering, on drain: the stops first, THEN the flag, THEN the placement-load
        decrements. The stops go through the :class:`AssignedServerStopper` Port and
        have COMMITTED when it returns, so flipping the in-memory flag afterwards
        keeps the two sides consistent: a failed commit rolls the servers back and
        leaves the Worker exactly as it was, rather than advertising a draining
        Worker whose servers are still ``desired=running`` — a state nothing
        converges. The decrements come last, after the flag rather than before it,
        so no interval advertises freed capacity on a Worker still eligible for
        placement (see :meth:`_shed_placement_load`).

        The registry lookup is what answers the unknown Worker, so the later
        :meth:`WorkerRegistry.set_draining` needs no second check: a registration is
        replaced on reconnect, never removed.
        """

        if self.registry.get(worker_id) is None:
            return None
        if not draining:
            self.registry.set_draining(worker_id, False)
            return 0
        stopped_ids = await self.stopper.stop_assigned(worker_id)
        self.registry.set_draining(worker_id, True)
        self._shed_placement_load(worker_id, stopped_ids)
        return len(stopped_ids)

    def _shed_placement_load(self, worker_id: WorkerId, server_ids: list[str]) -> None:
        """Drop each stopped server from the Worker's placement load.

        Drain owns the ``desired=running -> stopped`` flip, so it owns the decrement
        that pairs with it — mirroring ``StopServer.__call__`` (lifecycle.py). The
        in-memory load is "servers assigned with desired=running" (the tally rebuilt
        on reconnect via ``set_assignment``), so flipping desired=stopped without
        decrementing leaves the load inflated until the next reconnect. Only the
        flips the Port reports are shed, and those are exactly the ones that applied
        AND committed: a failed commit rolls its flips back and reports nothing, so
        no decrement leaks against a server still running. The reconciler's
        ``redispatch_stop`` deliberately does NOT decrement again (it assumes the
        original stop already did), so this is the single decrement for the drain
        path. ``decrement_assignment`` drops the per-server committed row; it is a
        harmless no-op when a same-instant reconnect rebuild already excluded this
        server (idempotent pop), the same self-healing race class StopServer's
        decrement carries.
        """

        for server_id in server_ids:
            self.registry.decrement_assignment(worker_id, server_id)
