"""The ``SetWorkerDrain`` use case: toggle a Worker's drain flag (FR-WRK-5).

A thin write use case behind the platform-admin endpoint. It holds only the
:class:`WorkerRegistry` Port so the HTTP edge and the registry adapter stay
decoupled. Drain is API-side state at M1: a draining Worker stays connected and
heartbeating but is excluded from placement.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.fleet.domain.registry import WorkerRegistry
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId


@dataclass(frozen=True)
class SetWorkerDrain:
    registry: WorkerRegistry

    def __call__(self, *, worker_id: WorkerId, draining: bool) -> None:
        self.registry.set_draining(worker_id, draining)
