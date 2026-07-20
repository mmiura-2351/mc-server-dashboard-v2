"""Background reconciler for desired/observed divergence and stale intents (#101).

The lifecycle layer commits intent then dispatches, compensating on a failed
dispatch (``lifecycle.py``). Two windows survive that design and are documented
there: (a) a crash between the commit and the dispatch leaves a durable intent
that was never sent; (b) a compensation failure leaves ``desired=running`` with no
assigned Worker. Both surface as the operator's intent (``desired_state``) not
matching the last Worker-reported reality (``observed_state``), and nothing acts
on them in-line. This reconciler closes the gap: each tick it finds the diverged
servers and replays the owed intent through the lifecycle use cases.

Divergence matrix (per candidate, after a grace window has lapsed):

- ``desired=running``, assigned Worker connected, observed not in
  ``{starting, running}`` -> re-dispatch the start (hydrate-then-start). Guarded
  to observed-not-running because the Worker's instance manager rejects a
  double-start with ``INVALID_STATE``; we only replay when the Worker is not
  already running it.
- ``desired=running``, no assigned Worker (compensation-failure orphan) -> run
  the normal placement + dispatch path. Assignment stickiness invariant (issue
  #101): once ``place_and_start`` has SENT a start command for this server it KEEPS
  the assignment even on a timeout/lost-response failure, so subsequent ticks take
  the ``redispatch_start`` path to the SAME Worker rather than re-placing on a
  different one. The reconciler never places a started server on a different Worker
  until an authoritative path (stop, worker-disconnect) clears the assignment —
  otherwise the Worker's per-process double-start guard would not catch a second
  live instance on another Worker.
- ``desired=stopped`` but observed ``running`` on a connected Worker -> re-dispatch
  the stop.
- ``desired=stopped``, observed ``stopped``, still assigned -> re-drive the final
  snapshot (if the Worker is connected) THEN clear the assignment (issue #847 bug 2,
  issue #1004). A stop whose deferred unassign never ran (an API crash or an
  HTTP-task cancellation mid final-snapshot) wedges here; no other path converges it
  (``StartServer`` 409s on ``require_unassigned`` before flipping desired, and the
  sink no longer unassigns). The snapshot leg dispatches to a connected Worker so the
  store generation advances before a cross-worker re-placement can hydrate; a
  disconnected Worker skips the snapshot and clears directly (same exposure as
  before, logged loud).
- ``desired=stopped``, observed ``unknown``, still assigned (issue #1599): a stop
  interrupted mid-flight (API restart maps STOPPING->unknown, or worker disconnect
  sets unknown without unassigning). Connected Worker -> redispatch the stop.
  Disconnected Worker -> clear the assignment (DB-only; same as the stopped wedge).
- Disconnected Worker -> skip (``desired=running`` side only):
  ``observed=unknown`` is expected while the Worker is gone, and the reconnect
  assignment rebuild owns that case (FR-WRK-4). The orphan path has no assigned
  Worker, so it is unaffected by this skip.

Grace window — a divergence is acted on only once it has persisted past a grace
(measured from ``max(updated_at, observed_at or updated_at)`` — the later of the
last intent commit and the last Worker report). This gives the normal in-flight
path time to converge (a start that is mid-launch reports ``starting``, not a
divergence) before the reconciler intervenes. The grace is per-action (issue
#999): a ``redispatch_start`` whose assigned Worker is connected AND already holds
a fresh-enough working set will SKIP hydrate (the same ``held >= store`` predicate
the lifecycle uses, #763), so it is command-only and gets the short
``held_start_grace_seconds`` — the full hydrate-based grace is pure dead waiting
there, and the cross-worker duplicate-live-instance race the long grace guards
cannot occur on a re-dispatch to the SAME connected Worker. Every other path
(``place_and_start``, a non-held ``redispatch_start`` that will hydrate, and both
stop-side actions) keeps the full ``grace_seconds``, preserving the #822
duplicate-start and #847 stale-snapshot floors.

Per-server exponential backoff — a failed action is not retried until a growing
window (``backoff_base_seconds`` doubled per consecutive failure, capped at
``backoff_max_seconds``) has lapsed, so a persistently failing server does not
thrash the fleet every tick. The backoff state is in-memory (a per-server map),
mirroring the snapshot scheduler's in-memory due-tracking: a restart forgets it,
which only means the first post-restart tick may retry sooner — acceptable, and
the divergence itself is still durable in the DB. A successful action clears the
entry — except a start re-dispatched at a server still observed ``crashed``, which
is a retry of a launch that evidently died: the dispatch succeeds (the Worker
launches the container) but the process crashes again, so it is counted as a
failure and backs off, damping the boot-crash loop (#343). Because each crash
cycle transits through ``starting`` (when the row briefly drops out of
``list_reconcilable``), the entry must NOT be cleared just because the server is
momentarily absent, or the failure count would reset every cycle and never grow.
Entries are therefore time-expired, not membership-cleaned: an entry is dropped
only once ``now`` is past ``next_eligible_at`` by ``backoff_max_seconds`` of slack
— so a genuinely healed server (which stops refreshing its entry) expires quietly,
while a still-flapping server re-arrives and refreshes ``next_eligible_at`` long
before then, so its backoff keeps growing up to ``backoff_max_seconds``. The map
still does not grow without bound.

Loud structured logs accompany every action and every failure so an operator can
see the reconciler working (NFR-OBS-1). One bad action is logged and left for a
later tick; it never aborts the rest of the tick.

Per-server actions are dispatched concurrently with a small cap (issue #871).
Processing servers serially head-of-line blocked the whole fleet: a single
legitimate big-world re-hydrate redispatch can hold the lifecycle path for up to
the hydrate budget (~600s, #822/#868), and a stop-side final snapshot for as long
again (#869), delaying every other server's convergence by that whole window.
Each action therefore runs on its OWN unit of work (a fresh use case from a
factory) — required for correctness, because the real ``UnitOfWork`` binds a
single mutable session that two concurrent actions would corrupt, and a bonus for
failure isolation: one action's rolled-back transaction can no longer poison
another's. The fan-out is bounded by a semaphore so the reconciler cannot stampede
the DB pool or the Worker fleet.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.application.lifecycle import (
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.control_plane import ControlPlane
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.store_generation import (
    StoreGenerationReader,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    DesiredState,
    ObservedState,
    ServerId,
)

_LOG = logging.getLogger(__name__)

# Observed states under a running intent that mean the server is NOT (yet)
# running: a re-dispatch of start is owed and the Worker will accept it (a
# double-start on a live instance is rejected with INVALID_STATE).
_NOT_RUNNING = (
    ObservedState.STOPPED,
    ObservedState.STOPPING,
    ObservedState.RESTARTING,
    ObservedState.CRASHED,
    ObservedState.UNKNOWN,
)

# Cap on how many per-server actions dispatch concurrently in one tick (#871).
# Small and constant: enough to stop one slow redispatch (a ~600s hydrate or a
# stop-side final snapshot) head-of-line blocking the rest of the fleet, but low
# enough that the reconciler never stampedes the DB connection pool or floods the
# Worker fleet with simultaneous hydrate/snapshot work. Not configurable — there
# is no operational case for tuning a background self-heal's burst width, and a
# knob here would just be one more thing to misset.
_MAX_CONCURRENT_ACTIONS = 4


@dataclass
class _Backoff:
    failures: int
    next_eligible_at: dt.datetime


@dataclass
class RunReconcilerTick:
    """One pass of the periodic divergence reconciler (issue #101).

    Not frozen: it owns the in-memory per-server backoff map mutated across ticks.
    A single instance is reused for the lifetime of the lifespan loop.
    """

    uow: UnitOfWork
    make_start_server: Callable[[], StartServer]
    make_stop_server: Callable[[], StopServer]
    control_plane: ControlPlane
    store_generation: StoreGenerationReader
    clock: Clock
    grace_seconds: int
    held_start_grace_seconds: int
    backoff_base_seconds: int
    backoff_max_seconds: int
    _attempts: dict[ServerId, _Backoff] = field(default_factory=dict)

    async def tick(self) -> None:
        now = self.clock.now()
        async with self.uow:
            candidates = await self.uow.servers.list_reconcilable()
        self._expire_stale(now)
        # Dispatch the per-server actions concurrently, capped (#871), so one slow
        # redispatch no longer head-of-line blocks the rest of the fleet. Each
        # candidate is distinct here (one row per server in list_reconcilable), so
        # concurrent actions never touch the same server; the only shared state is
        # the in-memory ``_attempts`` map, mutated only between awaits on this one
        # event loop, so a plain dict needs no lock. return_exceptions=True is
        # load-bearing: without it, the first escaped exception would cause gather to
        # return early while sibling tasks keep running past the tick boundary. With
        # it, tick() returns only after every action settles. Results are collected
        # so any Exception that escapes _consider's own handling is logged rather
        # than silently discarded (NFR-OBS-1).
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_ACTIONS)

        async def _bounded(server: Server) -> None:
            async with semaphore:
                await self._consider(server, now)

        pairs = [(server, _bounded(server)) for server in candidates]
        results = await asyncio.gather(
            *(coro for _, coro in pairs), return_exceptions=True
        )
        for server, result in zip((s for s, _ in pairs), results):
            if isinstance(result, Exception):
                _LOG.exception(
                    "unhandled exception in reconciler tick for server %s",
                    server.id.value,
                    exc_info=result,
                )

    def _expire_stale(self, now: dt.datetime) -> None:
        # Time-based expiry so the map does not grow without bound, WITHOUT
        # membership-based cleanup. A crash-looping server flaps observed
        # crashed -> starting -> crashed; while starting it drops out of
        # list_reconcilable, so a membership check ("absent now") would erase its
        # backoff every cycle and reset the failure count — defeating the
        # exponential growth (#343). Instead an entry survives until well past its
        # own next-eligible instant: a flapping server re-arrives long before then
        # and refreshes next_eligible_at (keeping its count), while a genuinely
        # healed server stops refreshing and its entry quietly expires. The slack
        # (backoff_max_seconds) comfortably exceeds the longest plausible absence
        # of a still-diverged server (a slow modded boot sitting in starting).
        expired = [
            server_id
            for server_id, attempt in self._attempts.items()
            if now
            >= attempt.next_eligible_at + dt.timedelta(seconds=self.backoff_max_seconds)
        ]
        for server_id in expired:
            del self._attempts[server_id]

    async def _consider(self, server: Server, now: dt.datetime) -> None:
        action = self._action_for(server)
        if action is None:
            return
        grace = await self._grace_for(server, action)
        if self._within_grace(server, now, grace):
            return
        attempt = self._attempts.get(server.id)
        if attempt is not None and now < attempt.next_eligible_at:
            return
        await self._run(server, action, now)

    async def _grace_for(self, server: Server, action: str) -> int:
        # Select the grace per action and held state (issue #999). A
        # ``redispatch_start`` whose assigned Worker is connected (already true here —
        # ``_action_for`` only returns it for a connected Worker) AND already holds a
        # fresh-enough working set will SKIP hydrate, so the start is command-only:
        # the full hydrate-based grace would be pure dead waiting, and the
        # cross-worker duplicate-live-instance race the long grace guards cannot occur
        # on a re-dispatch to the SAME connected Worker (its double-start guard
        # rejects a second live start). Use the short held-start grace there. Every
        # other path — ``place_and_start`` (may place elsewhere, always hydrates), a
        # non-held ``redispatch_start`` (hydrate will run), and the stop-side actions —
        # keeps the full grace, preserving the #822/#847 safety floors.
        if action != "redispatch_start" or server.assigned_worker_id is None:
            return self.grace_seconds
        store_generation = await self.store_generation.current_generation(
            community_id=server.community_id, server_id=server.id
        )
        if self.control_plane.holds_fresh_working_set(
            worker_id=server.assigned_worker_id,
            server_id=server.id,
            store_generation=store_generation,
        ):
            return self.held_start_grace_seconds
        return self.grace_seconds

    def _within_grace(self, server: Server, now: dt.datetime, grace: int) -> bool:
        # Measure from the most recent of observed_at (last Worker report) and
        # updated_at (last intent commit). update_lifecycle refreshes updated_at
        # but NOT observed_at, so a fresh start on a long-stale server would
        # otherwise get zero grace and race the in-flight start (#774).
        since = max(server.updated_at, server.observed_at or server.updated_at)
        return (now - since) < dt.timedelta(seconds=grace)

    def _action_for(self, server: Server) -> str | None:
        """Map a candidate to its reconciling action, or ``None`` to skip."""

        if server.desired_state is DesiredState.RUNNING:
            if server.assigned_worker_id is None:
                return "place_and_start"
            if server.observed_state not in _NOT_RUNNING:
                return None  # already running/starting: not actionable
            if not self.control_plane.is_worker_connected(
                worker_id=server.assigned_worker_id
            ):
                return None  # disconnected: reconnect rebuild owns it
            return "redispatch_start"
        # desired=stopped: list_reconcilable returns observed=running (stop never
        # delivered), observed=stopped+assigned (a stop wedged mid final-snapshot,
        # issue #847 bug 2), and observed=unknown+assigned (a stop interrupted
        # mid-flight, issue #1599).
        if server.assigned_worker_id is None:
            return None
        if server.observed_state is ObservedState.STOPPED:
            # The wedge recovery re-drives the final snapshot (if the Worker is
            # connected, issue #1004) then clears the assignment. It runs even
            # when the Worker is gone — the snapshot is skipped but the clear
            # proceeds, exactly the crash case it recovers.
            return "clear_stale_assignment"
        if server.observed_state is ObservedState.UNKNOWN:
            # Issue #1599: API restart or worker disconnect interrupted a stop
            # mid-flight, leaving (stopped, unknown, assigned). If the worker is
            # gone, clear the assignment (DB-only, same as the stopped wedge); if
            # connected, redispatch the stop to converge.
            if not self.control_plane.is_worker_connected(
                worker_id=server.assigned_worker_id
            ):
                return "clear_stale_assignment"
            return "redispatch_stop"
        if not self.control_plane.is_worker_connected(
            worker_id=server.assigned_worker_id
        ):
            return None
        return "redispatch_stop"

    async def _run(self, server: Server, action: str, now: dt.datetime) -> None:
        _LOG.info(
            "reconciling diverged server %s: desired=%s observed=%s action=%s",
            server.id.value,
            server.desired_state.value,
            server.observed_state.value,
            action,
        )
        try:
            dispatched = await self._dispatch(server, action)
        except Exception as exc:  # noqa: BLE001 - never abort the tick
            self._record_failure(server.id, now)
            _LOG.warning(
                "reconcile action %s failed for server %s: %r; backing off",
                action,
                server.id.value,
                exc,
            )
            return
        # Key the crash check off the entity the lifecycle use case RETURNS, not the
        # stale list_reconcilable snapshot in `server`. redispatch_start loads its own
        # entity and, on an INVALID_STATE convergence, records observed=running on THAT
        # copy and returns it (lifecycle.py); the snapshot predates the dispatch and
        # still reads CRASHED. Reading the returned entity is what keeps a server that
        # genuinely converged to running from being miscounted as a crash here.
        if dispatched.observed_state is ObservedState.CRASHED:
            # A start re-dispatched at a server still observed CRASHED is a RETRY of
            # a launch that evidently died: the dispatch succeeds (the Worker
            # launches the container) but the process crashes again, so the row stays
            # reconcilable and the next tick would re-issue at full cadence forever
            # (boot-crash loop, #343). Count it as a failure so consecutive crash
            # restarts back off exponentially like dispatch failures do. The backoff
            # keeps GROWING across crash cycles even though each cycle transits
            # through starting (when the row drops out of list_reconcilable): the
            # entry is not membership-cleaned, only time-expired (_expire_stale), and
            # a flapping server re-arrives long before its expiry instant.
            self._record_failure(server.id, now)
            _LOG.warning(
                "reconcile action %s dispatched for crash-looping server %s; "
                "backing off",
                action,
                server.id.value,
            )
            return
        self._attempts.pop(server.id, None)
        _LOG.info(
            "reconcile action %s succeeded for server %s", action, server.id.value
        )

    async def _dispatch(self, server: Server, action: str) -> Server:
        # Build a FRESH use case per action so each runs on its own unit of work
        # (#871). The real UnitOfWork binds a single mutable session, so two
        # concurrent actions sharing one use case would corrupt each other's
        # session; a per-action UoW also isolates failures (one rolled-back
        # transaction cannot poison another's).
        #
        # Return the lifecycle's freshly-loaded entity so _run reads the post-dispatch
        # observed_state (e.g. running after an INVALID_STATE convergence), not the
        # stale list_reconcilable snapshot.
        if action == "place_and_start":
            return await self.make_start_server().place_and_start(
                community_id=server.community_id, server_id=server.id
            )
        elif action == "redispatch_start":
            return await self.make_start_server().redispatch_start(
                community_id=server.community_id, server_id=server.id
            )
        elif action == "clear_stale_assignment":
            return await self.make_stop_server().clear_stale_assignment(
                community_id=server.community_id, server_id=server.id
            )
        else:  # redispatch_stop
            return await self.make_stop_server().redispatch_stop(
                community_id=server.community_id, server_id=server.id
            )

    def _record_failure(self, server_id: ServerId, now: dt.datetime) -> None:
        previous = self._attempts.get(server_id)
        failures = (previous.failures if previous is not None else 0) + 1
        delay = min(
            self.backoff_base_seconds * (2 ** (failures - 1)),
            self.backoff_max_seconds,
        )
        self._attempts[server_id] = _Backoff(
            failures=failures,
            next_eligible_at=now + dt.timedelta(seconds=delay),
        )
