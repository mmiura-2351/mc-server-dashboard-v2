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
- Disconnected Worker -> skip: ``observed=unknown`` is expected while the Worker
  is gone, and the reconnect assignment rebuild owns that case (FR-WRK-4). The
  orphan path has no assigned Worker, so it is unaffected by this skip.

Grace window — a divergence is acted on only once it has persisted past
``grace_seconds`` (measured from ``observed_at``, or ``updated_at`` when the server
has never been reported). This gives the normal in-flight path time to converge
(a start that is mid-launch reports ``starting``, not a divergence) before the
reconciler intervenes.

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
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.application.lifecycle import (
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.control_plane import ControlPlane
from mc_server_dashboard_api.servers.domain.entities import Server
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
    start_server: StartServer
    stop_server: StopServer
    control_plane: ControlPlane
    clock: Clock
    grace_seconds: int
    backoff_base_seconds: int
    backoff_max_seconds: int
    _attempts: dict[ServerId, _Backoff] = field(default_factory=dict)

    async def tick(self) -> None:
        now = self.clock.now()
        async with self.uow:
            candidates = await self.uow.servers.list_reconcilable()
        self._expire_stale(now)
        for server in candidates:
            await self._consider(server, now)

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
        if self._within_grace(server, now):
            return
        attempt = self._attempts.get(server.id)
        if attempt is not None and now < attempt.next_eligible_at:
            return
        action = self._action_for(server)
        if action is None:
            return
        await self._run(server, action, now)

    def _within_grace(self, server: Server, now: dt.datetime) -> bool:
        # Measure from the most recent of observed_at (last Worker report) and
        # updated_at (last intent commit). update_lifecycle refreshes updated_at
        # but NOT observed_at, so a fresh start on a long-stale server would
        # otherwise get zero grace and race the in-flight start (#774).
        since = max(server.updated_at, server.observed_at or server.updated_at)
        return (now - since) < dt.timedelta(seconds=self.grace_seconds)

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
        # desired=stopped (list_reconcilable only returns observed=running here).
        if server.assigned_worker_id is None:
            return None
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
        # Return the lifecycle's freshly-loaded entity so _run reads the post-dispatch
        # observed_state (e.g. running after an INVALID_STATE convergence), not the
        # stale list_reconcilable snapshot.
        if action == "place_and_start":
            return await self.start_server.place_and_start(
                community_id=server.community_id, server_id=server.id
            )
        elif action == "redispatch_start":
            return await self.start_server.redispatch_start(
                community_id=server.community_id, server_id=server.id
            )
        else:  # redispatch_stop
            return await self.stop_server.redispatch_stop(
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
