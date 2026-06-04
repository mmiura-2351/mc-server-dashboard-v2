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
  the normal placement + dispatch path.
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
entry.

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
        live_ids = {server.id for server in candidates}
        # Drop backoff state for servers no longer diverged so the map does not
        # grow without bound.
        for stale in self._attempts.keys() - live_ids:
            del self._attempts[stale]
        for server in candidates:
            await self._consider(server, now)

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
        since = server.observed_at or server.updated_at
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
            await self._dispatch(server, action)
        except Exception as exc:  # noqa: BLE001 - never abort the tick
            self._record_failure(server.id, now)
            _LOG.warning(
                "reconcile action %s failed for server %s: %r; backing off",
                action,
                server.id.value,
                exc,
            )
            return
        self._attempts.pop(server.id, None)
        _LOG.info(
            "reconcile action %s succeeded for server %s", action, server.id.value
        )

    async def _dispatch(self, server: Server, action: str) -> None:
        if action == "place_and_start":
            await self.start_server.place_and_start(
                community_id=server.community_id, server_id=server.id
            )
        elif action == "redispatch_start":
            await self.start_server.redispatch_start(
                community_id=server.community_id, server_id=server.id
            )
        else:  # redispatch_stop
            await self.stop_server.redispatch_stop(
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
