"""In-memory test doubles for the fleet context (TESTING.md Section 4)."""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.fleet.domain.clock import Clock
from mc_server_dashboard_api.fleet.domain.entities import Worker
from mc_server_dashboard_api.fleet.domain.server_state_sink import ServerStateSink
from mc_server_dashboard_api.fleet.domain.value_objects import (
    DriverKind,
    HostResources,
    WorkerCapabilities,
    WorkerId,
)


class FakeClock(Clock):
    def __init__(self, now: dt.datetime) -> None:
        self._now = now

    def set(self, now: dt.datetime) -> None:
        self._now = now

    def now(self) -> dt.datetime:
        return self._now


class FakeServerStateSink(ServerStateSink):
    """Records control-plane reconciliation calls; configurable running tally.

    The servicer drives this on a StatusChange (observed-state cache), on
    disconnect (mark unknown), and on register (rebuild assignment count).
    """

    def __init__(self, *, running_counts: dict[str, int] | None = None) -> None:
        self.observed: list[tuple[str, str, str]] = []
        self.unknown_for: list[str] = []
        self.counted_for: list[str] = []
        self._running_counts = running_counts or {}

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str
    ) -> None:
        self.observed.append((server_id, worker_id, state))

    async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
        self.unknown_for.append(worker_id)

    async def count_running_assignments(self, *, worker_id: str) -> int:
        self.counted_for.append(worker_id)
        return self._running_counts.get(worker_id, 0)


def make_worker(
    *,
    worker_id: str = "worker-1",
    version: str = "1.0.0",
    at: dt.datetime,
    drivers: frozenset[DriverKind] = frozenset({DriverKind.HOST_PROCESS}),
    max_servers: int = 4,
) -> Worker:
    return Worker(
        id=WorkerId(worker_id),
        version=version,
        capabilities=WorkerCapabilities(
            drivers=drivers,
            max_servers=max_servers,
            resources=HostResources(cpu_cores=8, memory_bytes=16_000_000_000),
        ),
        registered_at=at,
        last_heartbeat_at=at,
    )
