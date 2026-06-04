"""The ``WorkerRegistry`` Port (ARCHITECTURE.md Section 5.1).

The registry is the API's live record of connected Workers and their liveness
(FR-WRK-2, FR-WRK-4). The gRPC edge feeds it (register on the first message,
heartbeat on each ``Event{Heartbeat}``, disconnect when the stream ends); the
platform-admin read endpoint queries it.

The control plane keeps no cross-stream session state (CONTROL_PLANE.md Section
4.4): each connect is a clean registration that replaces any prior record for
the same ``worker_id``. The registry holds only currently/recently connected
Workers; the authoritative desired state lives elsewhere.
"""

from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass

from mc_server_dashboard_api.fleet.domain.entities import Worker, WorkerStatus
from mc_server_dashboard_api.fleet.domain.value_objects import (
    WorkerCapabilities,
    WorkerId,
)


@dataclass(frozen=True)
class WorkerSnapshot:
    """A read view of a registered Worker with its liveness resolved at read time."""

    id: WorkerId
    version: str
    capabilities: WorkerCapabilities
    registered_at: dt.datetime
    last_heartbeat_at: dt.datetime
    status: WorkerStatus


class WorkerRegistry(abc.ABC):
    """Port: the live registry of connected Workers and their liveness."""

    @abc.abstractmethod
    def register(self, worker: Worker) -> None:
        """Add or replace the record for ``worker.id`` (FR-WRK-1)."""

    @abc.abstractmethod
    def record_heartbeat(self, worker_id: WorkerId, at: dt.datetime) -> None:
        """Refresh the Worker's liveness to heartbeat time ``at`` (FR-WRK-2).

        A heartbeat for an unknown Worker is ignored.
        """

    @abc.abstractmethod
    def mark_disconnected(self, worker_id: WorkerId) -> None:
        """Mark the Worker offline because its stream ended (FR-WRK-4).

        A disconnect for an unknown Worker is ignored.
        """

    @abc.abstractmethod
    def list_workers(self) -> list[WorkerSnapshot]:
        """Return every registered Worker with its liveness resolved now."""
