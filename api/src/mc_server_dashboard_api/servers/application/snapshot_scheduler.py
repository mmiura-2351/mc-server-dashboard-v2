"""Periodic snapshot scheduler and the on-demand snapshot hook (FR-DATA-5/7).

The API drives snapshot cadence (the Worker self-addresses no Storage; only the
API knows the (community, server) scope). Two surfaces live here:

- :class:`RunSnapshotCadenceTick` — the periodic scheduler's one tick. The edge
  runs it on a loop as a lifespan task (like the gRPC server). Each tick lists
  the running, Worker-assigned servers, and dispatches a SnapshotTrigger to those
  that are due, honouring the per-server interval and a deterministic jitter. A
  published snapshot also refreshes the held-working-set inventory (issue #2477):
  the publish advances the authoritative store generation, so without it the
  snapshot itself would make the Worker's live scratch read back as stale.

- :class:`SnapshotServer` — an on-demand snapshot of one server, an internal use
  case the backup epic (#9) calls (save-all -> snapshot -> archive). No HTTP
  surface is mounted here; the issue keeps the on-demand path minimal.

Due-tracking is **in-memory** on the scheduler (a per-server next-due map), not a
persisted column: DATABASE.md Section 7 carries no last-snapshot timestamp, and
adding one was out of scope. The honest consequence is that a process restart
forgets the schedule, so every running server is re-snapshotted once shortly
after startup (within a jitter window) before settling back into its interval.
That keeps the RPO bounded (FR-DATA-5) at the cost of one extra, idempotent
snapshot per server per restart — an acceptable M1 trade.

Failures (a disconnected Worker, a refused trigger, a transport error) are logged
and left for the next tick: the server's next-due instant is advanced only on a
successful snapshot, so a failure is naturally retried, bounding the RPO by the
interval rather than dropping the snapshot.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.application.command_dispatch import (
    dispatch_failure,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.control_plane import (
    ControlPlane,
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidSnapshotIntervalError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.snapshot_cadence import (
    effective_interval_seconds,
    jitter_seconds,
    override_from_config,
)
from mc_server_dashboard_api.servers.domain.store_generation import (
    StoreGenerationReader,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)

_LOG = logging.getLogger(__name__)


@dataclass
class RunSnapshotCadenceTick:
    """One pass of the periodic snapshot scheduler (FR-DATA-7).

    Not frozen: it owns the in-memory next-due map mutated across ticks. A single
    instance is reused for the lifetime of the lifespan loop.
    """

    uow: UnitOfWork
    control_plane: ControlPlane
    clock: Clock
    store_generation: StoreGenerationReader
    default_interval_seconds: int
    min_interval_seconds: int
    # Per-server next-due instant; absence means "not yet scheduled".
    _next_due: dict[ServerId, dt.datetime] = field(default_factory=dict)

    async def tick(self) -> None:
        now = self.clock.now()
        async with self.uow:
            servers = await self.uow.servers.list_running_assigned()
        live_ids = {server.id for server in servers}
        # Forget servers that are no longer running/assigned so the map does not
        # grow without bound; a server that comes back is re-scheduled afresh.
        for stale in self._next_due.keys() - live_ids:
            del self._next_due[stale]
        for server in servers:
            await self._consider(server, now)

    async def _consider(self, server: Server, now: dt.datetime) -> None:
        interval = self._interval_for(server)
        if interval is None:
            return
        due_at = self._next_due.get(server.id)
        if due_at is None:
            # First time we see this server: schedule its first snapshot a jitter
            # offset out so a fleet sharing one interval does not fire in lockstep.
            self._next_due[server.id] = now + dt.timedelta(
                seconds=jitter_seconds(server.id, interval_seconds=interval)
            )
            return
        if now < due_at:
            return
        assert server.assigned_worker_id is not None  # running-assigned invariant
        if not self.control_plane.is_worker_connected(
            worker_id=server.assigned_worker_id
        ):
            # The assigned Worker is gone; skip without advancing next-due so the
            # snapshot is retried once it reconnects (FR-WRK-4, FR-DATA-5).
            return
        if await self._dispatch(server):
            # Reschedule one interval out (plus jitter) from now only on success;
            # a failure leaves next-due in the past so the next tick retries.
            self._next_due[server.id] = now + dt.timedelta(
                seconds=interval + jitter_seconds(server.id, interval_seconds=interval)
            )

    def _interval_for(self, server: Server) -> int | None:
        try:
            override = override_from_config(
                server.config, floor=self.min_interval_seconds
            )
        except InvalidSnapshotIntervalError:
            # A persisted override below the floor should be impossible (the update
            # use case validates on write), but if one slips in, skip rather than
            # crash the whole tick; log it for an operator to fix.
            _LOG.warning(
                "server %s has an invalid snapshot interval override; skipping",
                server.id.value,
            )
            return None
        return effective_interval_seconds(
            override=override,
            default=self.default_interval_seconds,
            floor=self.min_interval_seconds,
        )

    async def _dispatch(self, server: Server) -> bool:
        assert server.assigned_worker_id is not None
        try:
            outcome = await self.control_plane.snapshot(
                worker_id=server.assigned_worker_id,
                community_id=server.community_id,
                server_id=server.id,
            )
        except WorkerUnavailableError:
            _LOG.warning(
                "periodic snapshot could not reach the Worker for server %s; "
                "will retry next tick",
                server.id.value,
            )
            return False
        if not outcome.success:
            _LOG.warning(
                "periodic snapshot failed for server %s: %s; will retry next tick",
                server.id.value,
                outcome.message or outcome.status.value,
            )
            return False
        await self._refresh_held_working_set(server)
        return True

    async def _refresh_held_working_set(self, server: Server) -> None:
        """Record the published generation as held by the assigned Worker (#2477).

        A periodic snapshot advances the authoritative store generation past whatever
        the held-working-set inventory says, so without this refresh the very act of
        snapshotting makes the Worker look stale: the next start hydrates (rolling the
        live scratch back to the snapshot it just took) and the reconciler falls back
        from ``held_start_grace_seconds`` to the full grace.

        Recording is gated on the store's current generation still being OURS. The
        publisher check is what makes the claim provable rather than probable: a
        generation published by this Worker came FROM this Worker's scratch, so the
        scratch is at least as fresh as the store. Anything else — an at-rest edit
        (#889), a restore (#873), another Worker — advanced the store past what this
        Worker holds, and recording it would let a later start SKIP a hydrate it needs
        and boot the superseded world. The generation is read FIRST so a publish landing
        between the two reads can only pair an older generation with a newer publisher
        (understate or refuse), never the reverse.

        Only the PERIODIC path refreshes the inventory. It runs over
        ``list_running_assigned``, so the Worker demonstrably has a live instance and
        keeps its scratch. A stopped-id snapshot — the post-stop final snapshot in
        ``lifecycle._final_snapshot``, or the on-demand ``SnapshotServer`` against an
        at-rest set — is exactly the case where the Worker GCs its scratch right after
        publishing (instancemanager ``removeScratch``, #762/#841) and deliberately does
        not advance its own generation marker either: it then holds NOTHING. Those
        publishes stay unrecorded, and because they bump the store past any earlier
        recorded value, the inventory goes stale on its own and the next start hydrates.

        Best-effort: this is a mirror the Worker's next registration rebuilds from its
        own on-disk scan, so a Storage read failure must not turn a successful snapshot
        into a failed one (which would re-dispatch a snapshot that already published).
        """

        assert server.assigned_worker_id is not None
        try:
            generation = await self.store_generation.current_generation(
                community_id=server.community_id, server_id=server.id
            )
            publisher = await self.store_generation.current_publisher(
                community_id=server.community_id, server_id=server.id
            )
        except Exception:
            _LOG.warning(
                "could not read the published generation after snapshotting server %s; "
                "the held-working-set inventory keeps its previous value",
                server.id.value,
                exc_info=True,
            )
            return
        if publisher != str(server.assigned_worker_id.value):
            return
        self.control_plane.record_held_generation(
            worker_id=server.assigned_worker_id,
            server_id=server.id,
            generation=generation,
        )


@dataclass(frozen=True)
class SnapshotServer:
    """On-demand snapshot of one server (FR-DATA-7, backup epic hook).

    An internal use case the backup epic (#9) calls; no HTTP surface is mounted
    here. Returns the server so a caller can chain on it. Raises
    :class:`ServerNotFoundError` for an unknown / cross-community server and
    :class:`WorkerUnavailableError` / :class:`CommandDispatchError` on a failed
    dispatch — surfaced to the caller rather than swallowed (unlike the periodic
    path, an on-demand snapshot is acted on synchronously).
    """

    uow: UnitOfWork
    control_plane: ControlPlane

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> Server:
        async with self.uow:
            server = await self.uow.servers.get_by_id(server_id)
            if server is None or server.community_id != community_id:
                raise ServerNotFoundError(str(server_id.value))
            if server.assigned_worker_id is None:
                # No Worker holds the working set; nothing to snapshot.
                raise ServerNotFoundError(str(server_id.value))
            worker_id = server.assigned_worker_id

        outcome = await self.control_plane.snapshot(
            worker_id=worker_id,
            community_id=community_id,
            server_id=server_id,
        )
        if not outcome.success:
            raise dispatch_failure(
                server_id=server_id, kind="SnapshotServer", outcome=outcome
            )
        return server
