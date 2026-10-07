"""RevokeSession use case: revoke one of the caller's sessions (issue #387).

Revokes a single refresh-token session the caller owns, stamping
``revoked_reason = 'user_revoked'`` so the revoked token is never graced in the
reuse window. The session is the token's whole rotation chain, ended under the
caller's session lock: the listed id may belong to a token that a refresh has
rotated since, or is rotating now, and the session lives on in its successor
(issue #3254). The revoke is scoped to the caller's id, so a session id owned by
another user (or an unknown id, or a session already ended) matches nothing: the
use case returns ``False`` and the edge maps that to 404, leaking neither
existence nor ownership.
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
