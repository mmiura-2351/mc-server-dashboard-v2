"""Periodic scheduled-backup scheduler (FR-BAK-3).

The API drives the backup cadence on a lifespan loop, mirroring the snapshot
scheduler (PR #114): one :meth:`RunBackupScheduleTick.tick` per loop iteration
lists the servers that carry a per-server schedule, and creates a backup for each
that is due, honouring the configured interval and a deterministic jitter.

**Candidate set — both states, unlike snapshots.** The snapshot scheduler only
considers *running, worker-assigned* servers (a snapshot needs a live worker to
stream from). A scheduled *backup* is different: an at-rest server is archived
directly from the authoritative Storage copy with **no worker involved**
(Section 6.9), so it must be backed up even when nothing is running. The tick
therefore iterates every scheduled server and lets :class:`CreateBackup` branch on
state — the running path (save-all -> snapshot -> archive) naturally fails when the
worker is disconnected, and that failure is just retried next tick.

Due-tracking is **in-memory** on the scheduler (a per-server next-due map), not a
persisted column: the schedule lives in ``server.config`` and DATABASE.md carries
no last-backup timestamp, mirroring the snapshot scheduler's honest trade. A
restart re-backs-up each scheduled server once shortly after startup (within a
jitter window) before settling into its interval.

Failures (a transitional server, a disconnected worker on the running path, a
refused save-all, a Storage error) are logged and left for the next tick: a
server's next-due instant advances only on a successful backup, so a failure is
naturally retried, bounding the cadence by the interval rather than dropping the
backup.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.application.backups import CreateBackup
from mc_server_dashboard_api.servers.domain.backup import BackupSource
from mc_server_dashboard_api.servers.domain.backup_schedule import (
    jitter_seconds,
    schedule_from_config,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidBackupScheduleError,
    ServerError,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import ServerId

_LOG = logging.getLogger(__name__)


@dataclass
class RunBackupScheduleTick:
    """One pass of the periodic scheduled-backup scheduler (FR-BAK-3).

    Not frozen: it owns the in-memory next-due map mutated across ticks. A single
    instance is reused for the lifetime of the lifespan loop.
    """

    uow: UnitOfWork
    create_backup: CreateBackup
    clock: Clock
    # Per-server next-due instant; absence means "not yet scheduled".
    _next_due: dict[ServerId, dt.datetime] = field(default_factory=dict)

    async def tick(self) -> None:
        now = self.clock.now()
        async with self.uow:
            servers = await self.uow.servers.list_all()
        scheduled = {
            server.id: server
            for server in servers
            if self._interval_for(server) is not None
        }
        # Forget servers that no longer carry a schedule so the map does not grow
        # without bound; a server that gets one again is re-scheduled afresh.
        for stale in self._next_due.keys() - scheduled.keys():
            del self._next_due[stale]
        for server in scheduled.values():
            await self._consider(server, now)

    async def _consider(self, server: Server, now: dt.datetime) -> None:
        interval = self._interval_for(server)
        if interval is None:
            return
        due_at = self._next_due.get(server.id)
        if due_at is None:
            # First time we see this server: schedule its first backup a jitter
            # offset out so a fleet sharing one interval does not fire in lockstep.
            self._next_due[server.id] = now + dt.timedelta(
                seconds=jitter_seconds(server.id, interval_seconds=interval)
            )
            return
        if now < due_at:
            return
        if await self._run(server):
            # Reschedule one interval out (plus jitter) from now only on success;
            # a failure leaves next-due in the past so the next tick retries.
            self._next_due[server.id] = now + dt.timedelta(
                seconds=interval + jitter_seconds(server.id, interval_seconds=interval)
            )

    def _interval_for(self, server: Server) -> int | None:
        try:
            return schedule_from_config(server.config)
        except InvalidBackupScheduleError:
            # A persisted schedule that fails validation should be impossible (the
            # update use case validates on write), but if one slips in, skip rather
            # than crash the whole tick; log it for an operator to fix.
            _LOG.warning(
                "server %s has an invalid backup schedule; skipping",
                server.id.value,
            )
            return None

    async def _run(self, server: Server) -> bool:
        try:
            await self.create_backup(
                community_id=server.community_id,
                server_id=server.id,
                source=BackupSource.SCHEDULED,
                created_by=None,
            )
        except ServerError as exc:
            # A transitional server, a disconnected worker on the running path, a
            # refused save-all, or a missing working set: log and retry next tick.
            _LOG.warning(
                "scheduled backup failed for server %s: %r; will retry next tick",
                server.id.value,
                exc,
            )
            return False
        return True
