"""In-memory ``WorkerRegistry`` adapter (ARCHITECTURE.md Section 5.1).

Holds the connected Workers in a process-local dict. At this scale
(NFR-SCALE-1) the fleet is small and a single API process owns every Worker
stream, so an in-memory map is sufficient; a future milestone can swap a shared
adapter behind the same Port without touching the gRPC edge or the read
endpoint. Liveness is re-derived on every read from the injected ``Clock`` and
the configured heartbeat timeout, so no background sweep is needed.

Drain intent outlives connections: the registry remembers which worker ids an
operator has drained, so a re-registration of a drained id comes back DRAINING
rather than silently dropping the operator's intent when the Go agent
auto-reconnects. Only the DELETE drain endpoint clears that intent.

The adapter is shared across concurrent stream handlers on one event loop; its
operations are synchronous, non-blocking dict mutations, so no lock is required
under cooperative asyncio scheduling.
"""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.fleet.domain.clock import Clock
from mc_server_dashboard_api.fleet.domain.entities import Worker, WorkerStatus
from mc_server_dashboard_api.fleet.domain.placement import PlacementCandidate
from mc_server_dashboard_api.fleet.domain.registry import (
    SessionToken,
    WorkerRegistry,
    WorkerSnapshot,
)
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId


class InMemoryWorkerRegistry(WorkerRegistry):
    """Process-local :class:`WorkerRegistry` keyed by worker id."""

    def __init__(self, *, clock: Clock, heartbeat_timeout: dt.timedelta) -> None:
        self._clock = clock
        self._timeout = heartbeat_timeout
        self._workers: dict[WorkerId, Worker] = {}
        # The Session currently owning each worker id; a monotonic counter mints
        # fresh tokens so a reconnect always supersedes the prior Session.
        self._sessions: dict[WorkerId, SessionToken] = {}
        self._next_session: SessionToken = 0
        # Per-worker assigned-server count, the placement 'load' axis (FR-WRK-3).
        # Reset on (re)registration: a fresh connection starts with no servers
        # placed on it (epic #7 will re-place after hydrate).
        self._assignments: dict[WorkerId, int] = {}
        # Worker ids an operator has drained. This outlives connections so the
        # drain intent survives the Go agent's automatic reconnect; only the
        # DELETE drain endpoint clears it (FR-WRK-5).
        self._drained: set[WorkerId] = set()

    def register(self, worker: Worker) -> SessionToken:
        # Re-apply any standing drain intent: a re-registering worker that was
        # drained must come back DRAINING, not silently ONLINE.
        if worker.id in self._drained:
            worker = worker.start_draining()
        self._workers[worker.id] = worker
        # Assignment counts reset on (re)register; the server-lifecycle layer
        # (epic #7) MUST reconcile counts from worker status reports after
        # reconnect — a reconnected worker may still be running servers.
        self._assignments[worker.id] = 0
        session = self._next_session
        self._next_session += 1
        self._sessions[worker.id] = session
        return session

    def record_heartbeat(self, worker_id: WorkerId, at: dt.datetime) -> None:
        worker = self._workers.get(worker_id)
        if worker is not None:
            self._workers[worker_id] = worker.with_heartbeat(at)

    def mark_disconnected(self, worker_id: WorkerId, session: SessionToken) -> None:
        worker = self._workers.get(worker_id)
        # Ignore a teardown from a stale Session: the worker has reconnected on a
        # newer one and must stay online (CONTROL_PLANE.md Section 4.4).
        if worker is not None and self._sessions.get(worker_id) == session:
            self._workers[worker_id] = worker.disconnect()

    def set_draining(self, worker_id: WorkerId, draining: bool) -> bool:
        worker = self._workers.get(worker_id)
        if worker is None:
            return False
        if draining:
            self._drained.add(worker_id)
            self._workers[worker_id] = worker.start_draining()
        else:
            self._drained.discard(worker_id)
            self._workers[worker_id] = worker.stop_draining()
        return True

    def increment_assignment(self, worker_id: WorkerId) -> None:
        if worker_id in self._assignments:
            self._assignments[worker_id] += 1

    def decrement_assignment(self, worker_id: WorkerId) -> None:
        if self._assignments.get(worker_id, 0) > 0:
            self._assignments[worker_id] -= 1

    def set_assignment(self, worker_id: WorkerId, count: int) -> None:
        if worker_id in self._assignments:
            self._assignments[worker_id] = count

    def candidates_for_placement(self) -> list[PlacementCandidate]:
        now = self._clock.now()
        return [
            PlacementCandidate(
                worker_id=worker.id,
                drivers=worker.capabilities.drivers,
                capacity=worker.capabilities.max_servers,
                load=self._assignments[worker.id],
            )
            for worker in self._workers.values()
            if worker.status(now=now, timeout=self._timeout) is WorkerStatus.ONLINE
        ]

    def list_workers(self) -> list[WorkerSnapshot]:
        now = self._clock.now()
        return [self._snapshot(worker, now=now) for worker in self._workers.values()]

    def get(self, worker_id: WorkerId) -> WorkerSnapshot | None:
        worker = self._workers.get(worker_id)
        if worker is None:
            return None
        return self._snapshot(worker, now=self._clock.now())

    def _snapshot(self, worker: Worker, *, now: dt.datetime) -> WorkerSnapshot:
        return WorkerSnapshot(
            id=worker.id,
            version=worker.version,
            capabilities=worker.capabilities,
            registered_at=worker.registered_at,
            last_heartbeat_at=worker.last_heartbeat_at,
            status=worker.status(now=now, timeout=self._timeout),
            assigned_count=self._assignments[worker.id],
        )
