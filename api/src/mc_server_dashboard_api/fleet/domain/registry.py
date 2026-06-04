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
from mc_server_dashboard_api.fleet.domain.placement import PlacementCandidate
from mc_server_dashboard_api.fleet.domain.value_objects import (
    WorkerCapabilities,
    WorkerId,
)

# An opaque per-registration token. Each ``register`` call mints a fresh token
# identifying that Session's record; ``mark_disconnected`` carries it back so a
# stale stream's teardown only offlines the Worker if its Session is still the
# current one (CONTROL_PLANE.md Section 4.4, reconnect race).
SessionToken = int


@dataclass(frozen=True)
class WorkerSnapshot:
    """A read view of a registered Worker with its liveness resolved at read time.

    ``assigned_count`` is the Worker's current load: the number of servers
    assigned to it (the placement 'load' axis, FR-WRK-3). At M1 it is tracked by
    the registry via :meth:`WorkerRegistry.increment_assignment` /
    :meth:`WorkerRegistry.decrement_assignment`.
    """

    id: WorkerId
    version: str
    capabilities: WorkerCapabilities
    registered_at: dt.datetime
    last_heartbeat_at: dt.datetime
    status: WorkerStatus
    assigned_count: int


class WorkerRegistry(abc.ABC):
    """Port: the live registry of connected Workers and their liveness."""

    @abc.abstractmethod
    def register(self, worker: Worker) -> SessionToken:
        """Add or replace the record for ``worker.id`` (FR-WRK-1).

        Return a fresh :data:`SessionToken` identifying this registration; the
        caller passes it back to :meth:`mark_disconnected` so a stale Session's
        teardown cannot offline a Worker that has since reconnected.
        """

    @abc.abstractmethod
    def record_heartbeat(self, worker_id: WorkerId, at: dt.datetime) -> None:
        """Refresh the Worker's liveness to heartbeat time ``at`` (FR-WRK-2).

        A heartbeat for an unknown Worker is ignored.
        """

    @abc.abstractmethod
    def mark_disconnected(self, worker_id: WorkerId, session: SessionToken) -> None:
        """Mark the Worker offline because its stream ended (FR-WRK-4).

        Only the Session that is still current for ``worker_id`` may offline it:
        a disconnect whose ``session`` no longer matches the current record is
        ignored, so a stale stream's delayed teardown cannot offline a Worker
        that has reconnected on a newer Session (CONTROL_PLANE.md Section 4.4). A
        disconnect for an unknown Worker is ignored.
        """

    @abc.abstractmethod
    def set_draining(self, worker_id: WorkerId, draining: bool) -> bool:
        """Set or clear the Worker's drain flag (FR-WRK-5).

        A draining Worker stays connected and heartbeating but is excluded from
        placement. Clearing it (``draining=False``) makes the Worker eligible
        again. Returns ``True`` if the Worker was found, ``False`` otherwise, so
        the endpoint can map an unknown id to 404.
        """

    @abc.abstractmethod
    def increment_assignment(self, worker_id: WorkerId) -> None:
        """Record that one more server has been assigned to the Worker (load++).

        A call for an unknown Worker is ignored.
        """

    @abc.abstractmethod
    def decrement_assignment(self, worker_id: WorkerId) -> None:
        """Record that one server has left the Worker (load--, not below zero).

        A call for an unknown Worker is ignored.
        """

    @abc.abstractmethod
    def set_assignment(self, worker_id: WorkerId, count: int) -> None:
        """Set the Worker's assigned-server count to ``count`` (absolute load).

        Used to rebuild the count after a (re)registration reset it to zero: the
        lifecycle layer tallies the Worker's running servers from authoritative
        storage and writes the truth back here, so placement load is correct
        after a reconnect (epic #7 reconciliation obligation). A call for an
        unknown Worker is ignored.
        """

    @abc.abstractmethod
    def candidates_for_placement(self) -> list[PlacementCandidate]:
        """Return the placement-eligible Workers as :class:`PlacementCandidate`.

        Only ONLINE, non-draining Workers are included; each carries its
        advertised driver set and capacity plus its current load (assigned
        count). The pure :func:`place` function applies the driver/capacity
        filter and selection.
        """

    @abc.abstractmethod
    def list_workers(self) -> list[WorkerSnapshot]:
        """Return every registered Worker with its liveness resolved now."""
