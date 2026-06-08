"""ListSessions use case: the caller's active refresh-token sessions (issue #387).

Reads the caller's own active (unrevoked, unexpired) refresh tokens so the
session-management surface can render them. Read-only: it opens the unit of work,
queries, and never commits. Only safe metadata leaves the domain (id, issued_at,
expires_at); the token hash never does.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.entities import RefreshToken
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import UserId


@dataclass(frozen=True)
class ListSessions:
    """List the caller's active refresh-token sessions."""

    uow: UnitOfWork
    clock: Clock

    async def __call__(self, *, user_id: UserId) -> list[RefreshToken]:
        now = self.clock.now()
        async with self.uow:
            return await self.uow.refresh_tokens.list_active_for_user(user_id, now=now)
