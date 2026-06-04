"""In-memory ``WorkerRegistry`` adapter (ARCHITECTURE.md Section 5.1).

Holds the connected Workers in a process-local dict. At this scale
(NFR-SCALE-1) the fleet is small and a single API process owns every Worker
stream, so an in-memory map is sufficient; a future milestone can swap a shared
adapter behind the same Port without touching the gRPC edge or the read
endpoint. Liveness is re-derived on every read from the injected ``Clock`` and
the configured heartbeat timeout, so no background sweep is needed.

The adapter is shared across concurrent stream handlers on one event loop; its
operations are synchronous, non-blocking dict mutations, so no lock is required
under cooperative asyncio scheduling.
"""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.fleet.domain.clock import Clock
from mc_server_dashboard_api.fleet.domain.entities import Worker
from mc_server_dashboard_api.fleet.domain.registry import (
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

    def register(self, worker: Worker) -> None:
        self._workers[worker.id] = worker

    def record_heartbeat(self, worker_id: WorkerId, at: dt.datetime) -> None:
        worker = self._workers.get(worker_id)
        if worker is not None:
            self._workers[worker_id] = worker.with_heartbeat(at)

    def mark_disconnected(self, worker_id: WorkerId) -> None:
        worker = self._workers.get(worker_id)
        if worker is not None:
            self._workers[worker_id] = worker.disconnect()

    def list_workers(self) -> list[WorkerSnapshot]:
        now = self._clock.now()
        return [
            WorkerSnapshot(
                id=worker.id,
                version=worker.version,
                capabilities=worker.capabilities,
                registered_at=worker.registered_at,
                last_heartbeat_at=worker.last_heartbeat_at,
                status=worker.status(now=now, timeout=self._timeout),
            )
            for worker in self._workers.values()
        ]
