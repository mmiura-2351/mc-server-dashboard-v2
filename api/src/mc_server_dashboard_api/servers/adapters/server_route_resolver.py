"""Servers-backed adapter for the fleet :class:`ServerRouteResolver` Port.

The relay's ``ResolveJoin`` (a fleet adapter, RELAY.md Section 6) maps a slug to a
routing decision but must not reach into the servers domain (import-linter); this
edge module fulfils the fleet-domain Port against the servers repository, opening
its own transaction per call from the injected session factory (the RelayService
servicer has no request-scoped UnitOfWork — same shape as the state sink).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.fleet.domain.server_route_resolver import (
    ServerRoute,
    ServerRouteResolver,
)
from mc_server_dashboard_api.servers.adapters.repositories import (
    SqlAlchemyServerRepository,
)
from mc_server_dashboard_api.servers.domain.value_objects import ObservedState


class ServersServerRouteResolver(ServerRouteResolver):
    """:class:`ServerRouteResolver` adapter reading through the servers repository."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve_slug(self, slug: str) -> ServerRoute | None:
        async with self._session_factory() as session:
            repo = SqlAlchemyServerRepository(session)
            server = await repo.get_by_slug(slug)
        if server is None:
            return None
        return ServerRoute(
            server_id=str(server.id.value),
            display_name=server.name.value,
            is_running=server.observed_state is ObservedState.RUNNING,
            assigned_worker_id=(
                None
                if server.assigned_worker_id is None
                else str(server.assigned_worker_id.value)
            ),
        )
