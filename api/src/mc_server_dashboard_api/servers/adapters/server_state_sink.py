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


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
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

    async def record_observed_state(self, *, server_id: str, state: str) -> None:
        parsed = _parse_uuid(server_id)
        if parsed is None:
            return
        observed = ObservedState(state)
        async with self._session_factory() as session:
            repo = SqlAlchemyServerRepository(session)
            await repo.record_observed_state(
                ServerId(parsed), observed, self._clock.now()
            )
            await session.commit()

    async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
        parsed = _parse_uuid(worker_id)
        if parsed is None:
            return
        async with self._session_factory() as session:
            repo = SqlAlchemyServerRepository(session)
            await repo.mark_worker_servers_unknown(WorkerId(parsed), self._clock.now())
            await session.commit()

    async def count_running_assignments(self, *, worker_id: str) -> int:
        parsed = _parse_uuid(worker_id)
        if parsed is None:
            return 0
        async with self._session_factory() as session:
            repo = SqlAlchemyServerRepository(session)
            return await repo.count_running_for_worker(WorkerId(parsed))
