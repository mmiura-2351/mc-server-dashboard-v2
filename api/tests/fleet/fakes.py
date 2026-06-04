"""In-memory test doubles for the fleet context (TESTING.md Section 4)."""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.fleet.domain.clock import Clock
from mc_server_dashboard_api.fleet.domain.entities import Worker
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


def make_worker(
    *,
    worker_id: str = "worker-1",
    version: str = "1.0.0",
    at: dt.datetime,
) -> Worker:
    return Worker(
        id=WorkerId(worker_id),
        version=version,
        capabilities=WorkerCapabilities(
            drivers=frozenset({DriverKind.HOST_PROCESS}),
            max_servers=4,
            resources=HostResources(cpu_cores=8, memory_bytes=16_000_000_000),
        ),
        registered_at=at,
        last_heartbeat_at=at,
    )
