"""Periodic snapshot scheduler and the on-demand snapshot hook (FR-DATA-5/7).

The API drives snapshot cadence (the Worker self-addresses no Storage; only the
API knows the (community, server) scope). Two surfaces live here:

- :class:`RunSnapshotCadenceTick` — the periodic scheduler's one tick. The edge
  runs it on a loop as a lifespan task (like the gRPC server). Each tick lists
  the desired-running, Worker-assigned servers, and dispatches a SnapshotTrigger
  to those that are due, honouring the per-server interval and a deterministic
  jitter.

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

A dispatch that fails (a refused trigger, a transport error) is logged and
advances next-due exactly as a successful one does (issue #2485), so the failure
is retried on the server's own interval — still bounding the RPO by the interval
rather than dropping the snapshot. Holding next-due in the past instead retried at
the TICK period, which is the interval floor, so a persistent per-server failure
re-dispatched (and re-WARNed) every 300s for as long as it lasted. The one refusal
that is not a failure at all is the Worker's working_set_absent answer — it holds
nothing for this id, so nothing was lost and it is logged as such (issue #2480).

The one skip that does not advance next-due is the pre-dispatch connectivity gate:
nothing is dispatched and nothing is logged there, so retrying it every tick costs
nothing and the snapshot is taken as soon as the Worker reconnects.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.application.command_dispatch import (
    dispatch_failure,
)
from mc_server_dashboard_api.servers.application.lifecycle import (
    is_working_set_absent_refusal,
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
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)

_LOG = logging.getLogger(__name__)


@dataclass
class RunSnapshotCadenceTick:
    """One pass of the periodic snapshot scheduler (FR-DATA-7).

    The candidate set is DESIRED-running and assigned, with no observed-state
    predicate, and dispatching to a **crashed** member of it is a decision, not an
    oversight (issue #2480). The Worker has dropped the crashed instance from its
    map, so the trigger takes the at-rest branch over the retained working
    directory: it publishes, then GCs the scratch. Capturing that crash-time world
    is the point — under ``desired=running`` this tick is the only path that
    durably captures it. Everything else destroys or refuses it: the reconciler's
    automatic ``redispatch_start`` hydrates over the crash-window scratch and the
    displaced copy is swept on the next publish, a manual stop of a crashed server
    unassigns without a snapshot, and a backup of an unsettled server refuses. So
    skipping crashed servers here would leave their world captured by nothing.

    The scratch GC that follows is an intended consequence, and it is safe: the
    Worker removes the scratch only after the transfer reported success, which
    happens only once the API committed the snapshot (pinned Worker-side by
    ``TestStoppedSnapshotRemovesScratchAfterPublish`` and
    ``TestStoppedSnapshotFailureRetainsScratch``).

    Not frozen: it owns the in-memory next-due map mutated across ticks. A single
    instance is reused for the lifetime of the lifespan loop.
    """

    uow: UnitOfWork
    control_plane: ControlPlane
    clock: Clock
    default_interval_seconds: int
    min_interval_seconds: int
    # Per-server next-due instant; absence means "not yet scheduled".
    _next_due: dict[ServerId, dt.datetime] = field(default_factory=dict)

    async def tick(self) -> None:
        now = self.clock.now()
        async with self.uow:
            servers = await self.uow.servers.list_desired_running_assigned()
        live_ids = {server.id for server in servers}
        # Forget servers that are no longer desired-running/assigned so the map
        # does not grow without bound; one that comes back is re-scheduled afresh.
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
        assert server.assigned_worker_id is not None  # desired-running-assigned
        if not self.control_plane.is_worker_connected(
            worker_id=server.assigned_worker_id
        ):
            # The assigned Worker is gone; skip without advancing next-due so the
            # snapshot is retried once it reconnects (FR-WRK-4, FR-DATA-5).
            return
        await self._dispatch(server)
        # Reschedule whatever the dispatch answered: a failure is retried on this
        # server's own interval rather than on every tick (issue #2485).
        self._schedule_next(server, now)

    def _schedule_next(self, server: Server, now: dt.datetime) -> None:
        """Make ``server`` due one interval out (plus its jitter) from ``now``."""

        interval = self._interval_for(server)
        if interval is None:
            return
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

    async def _dispatch(self, server: Server) -> None:
        """Dispatch one snapshot and classify the answer in the log.

        Every outcome leaves next-due to advance (issue #2485); what differs is how
        the answer reads to an operator. A crashed server takes the Worker's at-rest
        branch here — publish, then scratch GC — which is the intended crash-time
        capture (issue #2480, see the class docstring). Its consequence is that the
        FIRST such tick captures the world and leaves the Worker holding nothing, so
        every later dispatch for the same id is refused with working_set_absent.
        That refusal is not a failure — there is nothing left to capture until the
        server starts again — so it must not WARN like one.
        """

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
                "will retry on the server's next interval",
                server.id.value,
            )
            return
        if is_working_set_absent_refusal(outcome):
            # Only the pinned working_set_absent phrase may be read this way; the
            # SERVER_NOT_FOUND code alone must not (issue #1790), so any other
            # SERVER_NOT_FOUND still takes the retry branch below.
            _LOG.info(
                "periodic snapshot for server %s found no working set to capture: "
                "the Worker holds nothing for this id; not retrying until it runs "
                "again",
                server.id.value,
            )
            return
        if not outcome.success:
            _LOG.warning(
                "periodic snapshot failed for server %s: %s; will retry on the "
                "server's next interval",
                server.id.value,
                outcome.message or outcome.status.value,
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
