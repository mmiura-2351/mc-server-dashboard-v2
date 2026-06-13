"""Use cases over recorded game sessions (RELAY.md Sections 8, 14; issue #957).

- :class:`ListGameSessions` — the ``session:read`` listing: a server's sessions
  newest-first, windowed by ``limit``/``offset``. Community-scoped (a session
  whose server is outside the path community is never returned — no
  cross-community signal, FR-COMM-3).
- :class:`PruneGameSessions` — the retention prune the background loop ticks:
  delete rows older than ``relay.session_retention_days`` (RELAY.md Section 8).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.errors import ServerNotFoundError
from mc_server_dashboard_api.servers.domain.game_session import GameSession
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)


@dataclass(frozen=True)
class ListGameSessions:
    """List a server's recorded sessions newest-first (``session:read``)."""

    uow: UnitOfWork

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        limit: int,
        offset: int,
    ) -> list[GameSession]:
        async with self.uow:
            server = await self.uow.servers.get_by_id(server_id)
            if server is None or server.community_id != community_id:
                raise ServerNotFoundError(str(server_id.value))
            return await self.uow.game_sessions.list_for_server(
                server_id, limit=limit, offset=offset
            )


@dataclass(frozen=True)
class PruneGameSessions:
    """Delete game sessions older than the retention window (RELAY.md Section 8)."""

    uow: UnitOfWork
    clock: Clock
    retention: dt.timedelta

    async def tick(self) -> int:
        """Delete sessions whose ``started_at`` is older than the window.

        Returns the number of rows deleted (for the loop's logging). Idempotent: a
        tick with nothing to prune deletes zero rows.
        """

        cutoff = self.clock.now() - self.retention
        async with self.uow:
            deleted = await self.uow.game_sessions.delete_started_before(cutoff)
            await self.uow.commit()
        return deleted
