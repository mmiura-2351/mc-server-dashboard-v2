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
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

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
    def register(
        self, worker: Worker, held_servers: Mapping[str, int] = MappingProxyType({})
    ) -> SessionToken:
        """Add or replace the record for ``worker.id`` (FR-WRK-1).

        Return a fresh :data:`SessionToken` identifying this registration; the
        caller passes it back to :meth:`mark_disconnected` so a stale Session's
        teardown cannot offline a Worker that has since reconnected.

        ``held_servers`` maps each server id whose working set the Worker reported
        it already holds in its persistent scratch to the GENERATION that set is at
        (issue #763). It is recorded so the lifecycle layer can skip the destructive
        hydrate on a same-worker restart ONLY when the held generation is fresh
        enough (see :meth:`held_generation`). A re-registration REPLACES the prior
        map — the control plane keeps no cross-stream session state (CONTROL_PLANE.md
        Section 4.4).
        """

    @abc.abstractmethod
    def held_generation(self, worker_id: WorkerId, server_id: str) -> int | None:
        """Return the generation ``worker_id`` reported holding for ``server_id``.

        Answers from the held map the Worker advertised on its current registration
        (issue #763). ``None`` when the Worker does not report holding the server
        (an unknown Worker, or one that re-registered without that id because its
        scratch was wiped or GC'd) — the lifecycle layer then hydrates rather than
        booting a server on an empty/absent working set. A held generation of 0
        means "held, but at generation 0" (an unknown / never-recorded generation):
        the lifecycle layer hydrates whenever the store generation is greater (the
        held set is stale), and skips only when the store generation is also 0 — a
        never-snapshotted server, where ``0 >= 0`` keeps the Worker's existing
        working set rather than hydrating an empty published set over it.
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
    def is_current_session(self, worker_id: WorkerId, session: SessionToken) -> bool:
        """Return whether ``session`` is still the current one for ``worker_id``.

        Lets a stale Session's delayed teardown tell itself apart from the live
        Session after a reconnect, so a teardown side effect that bypasses the
        per-server monotonic guard (the bulk observed=unknown write, FR-WRK-4)
        does not clobber the new Session's state (CONTROL_PLANE.md Section 4.4).
        Returns ``False`` for an unknown Worker.
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
    def reserve(self, worker_id: WorkerId, server_id: str, memory_mb: int) -> None:
        """Tentatively place ``server_id`` on ``worker_id`` before its commit (#778).

        Placement reserves a slot ATOMICALLY at decision time — in the same
        await-free section that read the candidates' load — so two concurrent
        starts cannot both read the same load/committed memory and both take a
        Worker's last capacity slot. The reservation counts toward placement load
        AND its ``memory_mb`` (0 = unset, not memory-gated) folds into the Worker's
        committed memory, so neither the count cap nor the memory gate is
        oversubscribed by the race. The reservation stands until the lifecycle layer
        either confirms it (:meth:`increment_assignment`, the commit landed) or
        releases it (:meth:`release_reservation`, the placement failed before
        commit). A call for an unknown Worker is ignored.
        """

    @abc.abstractmethod
    def reserved_memory_mb(self, worker_id: WorkerId) -> int:
        """Return the summed declared memory of ``worker_id``'s reservations (#778).

        The placement adapter folds this into the Worker's committed memory so the
        memory gate accounts for in-flight placements. ``0`` for an unknown Worker
        or one with no reservations.
        """

    @abc.abstractmethod
    def release_reservation(self, worker_id: WorkerId, server_id: str) -> None:
        """Drop the reservation for ``server_id`` made by :meth:`reserve` (#778).

        Called when a placement fails BEFORE the lifecycle commit (a lost
        compare-and-set), so the tentatively-held slot is freed without ever
        counting as a committed assignment. A call for an unknown Worker or an
        absent reservation is ignored.
        """

    @abc.abstractmethod
    def increment_assignment(self, worker_id: WorkerId, server_id: str) -> None:
        """Confirm ``server_id``'s reserved slot as a committed assignment (#778).

        Called after the lifecycle commit lands. Converts the reservation made by
        :meth:`reserve` into a committed assignment (load is unchanged — the
        reservation already counted it). If no reservation is outstanding for
        ``server_id``, this is a NO-OP: a re-registration between the commit and
        this call already rebuilt the count from the authoritative tally
        (:meth:`set_assignment`), which dropped this reservation because the
        committed row was in the tally — so incrementing again would double-count
        (#778). A call for an unknown Worker is ignored.
        """

    @abc.abstractmethod
    def decrement_assignment(self, worker_id: WorkerId) -> None:
        """Record that one server has left the Worker (load--, not below zero).

        A call for an unknown Worker is ignored.
        """

    @abc.abstractmethod
    def set_assignment(self, worker_id: WorkerId, server_ids: set[str]) -> None:
        """Rebuild the Worker's committed assignments from the authoritative tally.

        Used after a (re)registration reset the count to zero: the lifecycle layer
        tallies the server ids the Worker is running from authoritative storage and
        writes the truth back here, so placement load is correct after a reconnect
        (epic #7 reconciliation obligation). The committed count becomes
        ``len(server_ids)``; any RESERVED placement whose server is already in
        ``server_ids`` is dropped (the commit landed and is now counted in the
        tally, so its pending :meth:`increment_assignment` must become a no-op to
        avoid double-counting, #778), while a reservation NOT yet in the tally (its
        commit not yet visible) is kept so its later confirm still counts. A call
        for an unknown Worker is ignored.
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

    @abc.abstractmethod
    def get(self, worker_id: WorkerId) -> WorkerSnapshot | None:
        """Return the Worker's snapshot with liveness resolved now, or ``None``.

        A per-id accessor for liveness checks that would otherwise scan
        :meth:`list_workers`; returns ``None`` for an unknown Worker.
        """
