"""The ``Worker`` entity and its liveness model (FR-WRK-2, FR-WRK-4).

A registered Worker tracks its advertised capabilities and the time of its last
heartbeat. Liveness is derived, not stored: a Worker is ``ONLINE`` while its
last heartbeat is within the liveness window, and ``OFFLINE`` once heartbeats
lapse past it or the stream disconnects. Keeping liveness a pure function of
``last_heartbeat_at``, ``now`` and the timeout means a single registry sweep
re-derives the state of every Worker deterministically (TESTING.md Section 4).
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass, replace

from mc_server_dashboard_api.fleet.domain.value_objects import (
    WorkerCapabilities,
    WorkerId,
)


class WorkerStatus(enum.Enum):
    """A Worker's liveness as seen by the API."""

    ONLINE = "online"
    OFFLINE = "offline"


@dataclass(frozen=True)
class Worker:
    """A registered Worker and its last-known liveness signal.

    ``disconnected`` is set when the stream ends (clean close or transport
    error); such a Worker is ``OFFLINE`` regardless of its heartbeat age. A
    still-connected Worker is ``OFFLINE`` only once it misses heartbeats past the
    liveness window (CONTROL_PLANE.md Section 4.3/4.4).
    """

    id: WorkerId
    version: str
    capabilities: WorkerCapabilities
    registered_at: dt.datetime
    last_heartbeat_at: dt.datetime
    disconnected: bool = False

    def with_heartbeat(self, at: dt.datetime) -> Worker:
        """Return a copy whose liveness is refreshed to heartbeat time ``at``."""

        return replace(self, last_heartbeat_at=at, disconnected=False)

    def disconnect(self) -> Worker:
        """Return a copy marked disconnected (stream ended)."""

        return replace(self, disconnected=True)

    def status(self, *, now: dt.datetime, timeout: dt.timedelta) -> WorkerStatus:
        """Derive liveness at ``now`` for the given heartbeat ``timeout``."""

        if self.disconnected:
            return WorkerStatus.OFFLINE
        if now - self.last_heartbeat_at > timeout:
            return WorkerStatus.OFFLINE
        return WorkerStatus.ONLINE
