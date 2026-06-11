"""Servers-backed adapter for the fleet :class:`ServerStateSink` Port.

The control-plane event path (the gRPC servicer, a fleet adapter) reconciles
authoritative *server* state from worker reports — observed-state caching on a
``StatusChange``, observed=unknown on disconnect (FR-WRK-4), and the
running-server tally that rebuilds a reconnected worker's placement load (epic #7
obligation). The servicer depends on the fleet-domain Port; this adapter fulfils
it against the servers repository, opening its own transaction per call from the
injected session factory (the servicer has no request-scoped UnitOfWork).

This is an adapter-layer composition across contexts: a fleet Port implemented
with the servers repository. The servers *domain*/*application* never reach into
fleet (import-linter); only this edge module bridges the two.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.fleet.domain.server_state_sink import ServerStateSink
from mc_server_dashboard_api.servers.adapters.repositories import (
    SqlAlchemyServerRepository,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.value_objects import (
    ObservedState,
    ServerId,
    WorkerId,
)

_LOG = logging.getLogger(__name__)


def _parse_id(value: str, *, kind: str) -> uuid.UUID | None:
    """Parse an id the seam guarantees is a UUID, logging loudly on failure.

    Worker ids are enforced to be UUIDs at registration (issue #99) and server
    ids are DB-issued UUIDs, so a value that fails to parse here is an invariant
    violation at the control-plane seam. It is logged at ERROR (not silently
    skipped) so the broken bridging surfaces instead of dropping reports.
    """

    try:
        return uuid.UUID(value)
    except ValueError:
        _LOG.error(
            "control-plane %s is not a UUID; dropping report (invariant violation)",
            kind,
            extra={kind: value},
        )
        return None


class ServersServerStateSink(ServerStateSink):
    """:class:`ServerStateSink` adapter writing through the servers repository."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str
    ) -> None:
        parsed = _parse_id(server_id, kind="server_id")
        parsed_worker = _parse_id(worker_id, kind="worker_id")
        if parsed is None or parsed_worker is None:
            return
        observed = ObservedState(state)
        async with self._session_factory() as session:
            repo = SqlAlchemyServerRepository(session)
            server = await repo.get_by_id(ServerId(parsed))
            if server is None:
                return
            # Ownership guard: only the server's currently assigned worker may
            # write its observed state. A report from any other worker (stale or
            # misrouted) is dropped with a warning, not applied (defense-in-depth).
            if server.assigned_worker_id != WorkerId(parsed_worker):
                _LOG.warning(
                    "dropping status report from non-owning worker",
                    extra={
                        "server_id": server_id,
                        "reporting_worker_id": worker_id,
                        "assigned_worker_id": (
                            None
                            if server.assigned_worker_id is None
                            else str(server.assigned_worker_id.value)
                        ),
                    },
                )
                return
            # The sink NEVER unassigns from a status report (issue #847). The old
            # #217 sink-unassign (clear the assignment when the owning worker
            # reports stopped under desired=stopped) raced the final-snapshot
            # window: post-#847 the worker is STILL the owner while the final
            # snapshot uploads, so its terminal StatusChange(stopped) passed the
            # ownership guard above and released the assignment milliseconds into a
            # snapshot that can last minutes — reopening the stop->re-place
            # generation race the #847 hold exists to close. The stop flow now owns
            # the unassign end-to-end: StopServer clears the assignment only AFTER
            # the final snapshot settles, and the reconciler's stale-stop arm
            # recovers a row left wedged at (stopped, stopped, assigned) by a
            # crash/timeout mid-window (the deliberate replacement for #217's
            # recovery). So this sink only caches the observed state.
            await repo.record_observed_state(
                ServerId(parsed), observed, self._clock.now(), unassign=False
            )
            await session.commit()

    async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
        parsed = _parse_id(worker_id, kind="worker_id")
        if parsed is None:
            return
        async with self._session_factory() as session:
            repo = SqlAlchemyServerRepository(session)
            await repo.mark_worker_servers_unknown(WorkerId(parsed), self._clock.now())
            await session.commit()

    async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
        parsed = _parse_id(worker_id, kind="worker_id")
        if parsed is None:
            return {}
        async with self._session_factory() as session:
            repo = SqlAlchemyServerRepository(session)
            return await repo.running_assignment_ids_for_worker(WorkerId(parsed))
