"""CheckHealth use case: derive the health report from dependency liveness."""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.core.domain.health import DatabasePing, HealthReport


@dataclass(frozen=True)
class CheckHealth:
    """Probe dependencies and produce a :class:`HealthReport`.

    The service is healthy iff every checked dependency is reachable; in M1 that
    is the database alone.
    """

    database: DatabasePing

    async def __call__(self) -> HealthReport:
        reachable = await self.database.is_reachable()
        return HealthReport(ok=reachable, database_reachable=reachable)
