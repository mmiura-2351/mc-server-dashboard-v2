"""The ``ListWorkers`` use case: read the registered Workers and their liveness.

A thin read use case behind the platform-admin endpoint (FR-WRK-2 operability
surface). It holds only the :class:`WorkerRegistry` Port, so the HTTP edge and
the registry adapter stay decoupled.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.fleet.domain.registry import (
    WorkerRegistry,
    WorkerSnapshot,
)


@dataclass(frozen=True)
class ListWorkers:
    registry: WorkerRegistry

    def __call__(self) -> list[WorkerSnapshot]:
        return self.registry.list_workers()
