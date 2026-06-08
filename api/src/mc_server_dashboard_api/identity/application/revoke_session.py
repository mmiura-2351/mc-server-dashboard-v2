"""RevokeSession use case: revoke one of the caller's sessions (issue #387).

Revokes a single refresh-token session the caller owns, stamping
``revoked_reason = 'user_revoked'`` so the revoked token is never graced in the
reuse window. The revoke is scoped to the caller's id, so a session id owned by
another user (or an unknown id) matches no row: the use case returns ``False`` and
the edge maps that to 404, leaking neither existence nor ownership.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.entities import REVOKED_USER
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import (
    RefreshTokenId,
    UserId,
)


@dataclass(frozen=True)
class RevokeSession:
    """Revoke one refresh-token session the caller owns."""

    uow: UnitOfWork
    clock: Clock

    async def __call__(self, *, user_id: UserId, session_id: RefreshTokenId) -> bool:
        async with self.uow:
            revoked = await self.uow.refresh_tokens.revoke_by_id(
                session_id,
                user_id,
                revoked_at=self.clock.now(),
                reason=REVOKED_USER,
            )
            await self.uow.commit()
        return revoked
