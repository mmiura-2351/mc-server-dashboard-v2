"""In-memory ``WorkerRegistry`` adapter (ARCHITECTURE.md Section 5.1).

Holds the connected Workers in a process-local dict. At this scale
(NFR-SCALE-1) the fleet is small and a single API process owns every Worker
stream, so an in-memory map is sufficient; a future milestone can swap a shared
adapter behind the same Port without touching the gRPC edge or the read
endpoint. Liveness is re-derived on every read from the injected ``Clock`` and
the configured heartbeat timeout, so no background sweep is needed for
liveness reads; stream timeout enforcement lives in the per-session
watchdog coroutine in ``grpc_server.py`` (issue #1600).

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
from collections.abc import Mapping
from types import MappingProxyType

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
        # Per-worker COMMITTED assignments: server id -> declared memory request in
        # MiB (0 = unset / not memory-gated). This is BOTH the placement 'load' axis
        # (FR-WRK-3, the count is len) AND the committed-memory axis (#843). Tracking
        # committed memory HERE — rather than reading it from a DB snapshot taken one
        # await earlier — means the count and the memory gate are read in the SAME
        # synchronous section as the reservations, so the cross-axis race (a confirm
        # popping A's reserved memory between B's snapshot read and B's placement)
        # cannot leave a server's memory counted in NEITHER source. Reset on
        # (re)registration: a fresh connection starts with no servers placed on it;
        # the lifecycle layer rebuilds it from the authoritative tally via
        # set_assignment (epic #7 reconciliation).
        self._committed: dict[WorkerId, dict[str, int]] = {}
        # Monotonic confirm sequence and the sequence at which each committed id was
        # confirmed (#844). set_assignment (the reconnect rebuild) reads its DB tally
        # one await before it runs; a commit+confirm landing in that window stamps the
        # newly-committed id with a sequence past the snapshot's, so the rebuild keeps
        # it instead of letting the stale tally overwrite the +1.
        self._next_seq: int = 0
        self._confirmed_seq: dict[WorkerId, dict[str, int]] = {}
        # Per-worker DECREMENT tombstones: server id -> sequence at which the row was
        # decremented (#862). set_assignment uses this to detect a stop that landed
        # AFTER the snapshot epoch so it does not resurrect the row from the stale
        # tally (the symmetric fix to the confirm-side guard above).
        self._decremented_seq: dict[WorkerId, dict[str, int]] = {}
        # Per-worker RESERVED-but-uncommitted placements: server id -> declared
        # memory request in MiB (0 = unset / not memory-gated), the slot held
        # atomically at placement decision time (#778). A concurrent placement sees
        # each reservation counted toward load AND its memory folded into committed
        # memory, so neither the count cap nor the memory gate can be oversubscribed
        # by two starts racing for a Worker's last slot. The slot is confirmed (moved
        # into _committed) by increment_assignment after the lifecycle commit, or
        # released by release_reservation if the placement loses its commit race.
        self._reserved: dict[WorkerId, dict[str, int]] = {}
        # Worker ids an operator has drained. This outlives connections so the
        # drain intent survives the Go agent's automatic reconnect; only the
        # DELETE drain endpoint clears it (FR-WRK-5).
        self._drained: set[WorkerId] = set()
        # The working sets each connected Worker reported it already holds in its
        # persistent scratch at its current registration, mapped to the GENERATION
        # each is at (issue #763). Replaced on every (re)register, so it tracks only
        # the live session's reality; the lifecycle layer reads it via
        # held_generation to skip the destructive hydrate on a same-worker restart
        # only when the held generation is fresh enough.
        self._held: dict[WorkerId, dict[str, int]] = {}

    def register(
        self, worker: Worker, held_servers: Mapping[str, int] = MappingProxyType({})
    ) -> SessionToken:
        # Re-apply any standing drain intent: a re-registering worker that was
        # drained must come back DRAINING, not silently ONLINE.
        if worker.id in self._drained:
            worker = worker.start_draining()
        self._workers[worker.id] = worker
        # Replace the held-working-set inventory with what THIS registration
        # reported (issue #763): a reconnect whose scratch was wiped/GC'd reports
        # fewer ids, so a stale "held" claim never survives a re-register and the
        # lifecycle layer hydrates rather than booting an empty working set.
        self._held[worker.id] = dict(held_servers)
        # Committed assignments reset on (re)register; the server-lifecycle layer
        # (epic #7) MUST reconcile them from worker status reports after reconnect —
        # a reconnected worker may still be running servers. Reserved placements are
        # NOT reset: a placement reserved before the reconnect may still commit, and
        # set_assignment (the rebuild) drops only those already reflected in the
        # tally, leaving still-uncommitted ones pending so their later confirm counts
        # (#778). Ensure the keys exist for a brand-new worker.
        self._committed[worker.id] = {}
        self._confirmed_seq[worker.id] = {}
        self._decremented_seq[worker.id] = {}
        self._reserved.setdefault(worker.id, {})
        session = self._next_session
        self._next_session += 1
        self._sessions[worker.id] = session
        return session

    def held_generation(self, worker_id: WorkerId, server_id: str) -> int | None:
        return self._held.get(worker_id, {}).get(server_id)

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

    def is_current_session(self, worker_id: WorkerId, session: SessionToken) -> bool:
        return self._sessions.get(worker_id) == session

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

    def reserve(self, worker_id: WorkerId, server_id: str, memory_mb: int) -> None:
        if worker_id in self._committed:
            self._reserved[worker_id][server_id] = memory_mb

    def reserved_memory_mb(self, worker_id: WorkerId) -> int:
        return sum(self._reserved.get(worker_id, {}).values())

    def committed_memory_mb(self, worker_id: WorkerId) -> int:
        return sum(self._committed.get(worker_id, {}).values())

    def assignment_epoch(self, worker_id: WorkerId) -> int:
        return self._next_seq

    def release_reservation(self, worker_id: WorkerId, server_id: str) -> None:
        if worker_id in self._reserved:
            self._reserved[worker_id].pop(server_id, None)

    def increment_assignment(self, worker_id: WorkerId, server_id: str) -> None:
        if worker_id not in self._committed:
            return
        # Confirm the reservation -> committed, carrying its declared memory (#843)
        # so the committed-memory axis is maintained in lock-step with the count. If
        # the reservation is already gone, a rebuild (set_assignment) between the
        # commit and this call counted it in the tally and dropped the reservation,
        # so confirming again would double-count: treat the missing reservation as
        # already-counted (#778).
        memory_mb = self._reserved[worker_id].pop(server_id, None)
        if memory_mb is not None:
            self._committed[worker_id][server_id] = memory_mb
            # Stamp the confirm sequence so a concurrent rebuild whose tally was read
            # before this confirm keeps the row instead of overwriting it (#844).
            self._confirmed_seq[worker_id][server_id] = self._next_seq
            self._next_seq += 1

    def decrement_assignment(self, worker_id: WorkerId, server_id: str) -> None:
        # Drop the committed row (and its memory) for ``server_id`` (#843); idempotent
        # if it is already gone (e.g. a rebuild between the desired-state flip and
        # this call already excluded it), mirroring the count's floor-at-zero.
        # Stamp the current sequence so set_assignment can detect a decrement that
        # lands in the rebuild's tally-read window and not resurrect the row (#862).
        if worker_id in self._committed:
            self._committed[worker_id].pop(server_id, None)
            self._confirmed_seq[worker_id].pop(server_id, None)
            self._decremented_seq[worker_id][server_id] = self._next_seq
            self._next_seq += 1

    def set_assignment(
        self, worker_id: WorkerId, assignments: Mapping[str, int], snapshot_epoch: int
    ) -> None:
        if worker_id not in self._committed:
            return
        # Preserve any committed row whose confirm landed AFTER the snapshot the
        # caller's DB tally was read at (#844): the rebuild reads the tally one await
        # before it calls here, and a commit+confirm in that window is not yet visible
        # in ``assignments`` — without this it would be overwritten by the stale
        # tally and undercounted until the next reconnect.
        preserved = {
            server_id: memory_mb
            for server_id, memory_mb in self._committed[worker_id].items()
            if self._confirmed_seq[worker_id].get(server_id, -1) >= snapshot_epoch
        }
        # Exclude any tally row that was decremented AFTER the snapshot epoch (#862):
        # the stop fired in the tally-read window so the tally is stale — do not
        # resurrect the row by merging it from assignments.
        tombstoned = {
            server_id
            for server_id, seq in self._decremented_seq[worker_id].items()
            if seq >= snapshot_epoch
        }
        filtered_assignments = {
            server_id: memory_mb
            for server_id, memory_mb in assignments.items()
            if server_id not in tombstoned
        }
        self._committed[worker_id] = {**filtered_assignments, **preserved}
        self._confirmed_seq[worker_id] = {
            server_id: self._confirmed_seq[worker_id].get(server_id, -1)
            for server_id in self._committed[worker_id]
        }
        # Drop reservations now reflected in the authoritative tally so their pending
        # increment_assignment becomes a no-op (no double-count); keep reservations
        # not yet in the tally (their commit is not yet visible) so their later
        # confirm still counts (#778).
        for committed_id in filtered_assignments:
            self._reserved[worker_id].pop(committed_id, None)

    def candidates_for_placement(self) -> list[PlacementCandidate]:
        now = self._clock.now()
        return [
            PlacementCandidate(
                worker_id=worker.id,
                drivers=worker.capabilities.drivers,
                capacity=worker.capabilities.max_servers,
                # Load counts committed assignments PLUS reservations still in
                # flight, so a concurrent placement sees a tentatively-taken slot
                # and cannot oversubscribe a Worker's last capacity slot (#778).
                load=self._load(worker.id),
                # Advertised host memory for resource-aware placement (#710),
                # in MiB (the per-server limit's unit). 0 means the worker
                # advertised none, so the placement filter falls back to
                # count-only for it.
                memory_capacity_mb=worker.capabilities.resources.memory_bytes
                // (1024 * 1024),
                # Committed memory: the declared memory of confirmed assignments
                # PLUS in-flight reservations, read here in the same synchronous
                # section as the load count so the memory gate cannot be raced by a
                # confirm popping reserved memory between a DB snapshot and the
                # placement decision (#843). CPU stays a soft tie-break the adapter
                # folds from the DB snapshot.
                committed_memory_mb=self.committed_memory_mb(worker.id)
                + self.reserved_memory_mb(worker.id),
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
            # The read view reflects the same load placement sees: committed
            # assignments plus in-flight reservations (#778).
            assigned_count=self._load(worker.id),
        )

    def _load(self, worker_id: WorkerId) -> int:
        """Placement load: committed assignments plus in-flight reservations (#778)."""

        return len(self._committed[worker_id]) + len(self._reserved.get(worker_id, ()))
