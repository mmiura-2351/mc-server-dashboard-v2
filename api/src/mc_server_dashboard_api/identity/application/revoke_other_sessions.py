"""RevokeOtherSessions use case: everywhere-else logout (issue #387, #606).

Revokes all the caller's active refresh-token sessions except their current one,
stamping ``revoked_reason = 'user_revoked'``. The current session can be
identified two ways:

- **By refresh token** (``current_refresh_token``): hashed and matched against
  the stored rows. Original mechanism.
- **By session id** (``keep_session_id``): the row id returned by ``GET
  /users/me/sessions``. Added in issue #606 so the SPA (whose refresh cookie is
  ``/api/auth``-confined) can keep its current session without echoing the token.

If neither is provided, there is no row to spare: every active session is
revoked, including the current one. This is the safe choice -- it never revokes
*another* user's sessions.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.entities import REVOKED_USER
from mc_server_dashboard_api.identity.domain.token_service import TokenService
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import RefreshTokenId, UserId


@dataclass(frozen=True)
class RevokeOtherSessions:
    """Revoke all the caller's sessions except the one they identified."""

    uow: UnitOfWork
    tokens: TokenService
    clock: Clock

    async def __call__(
        self,
        *,
        user_id: UserId,
        current_refresh_token: str | None,
        keep_session_id: RefreshTokenId | None = None,
    ) -> None:
        keep_hash = (
            self.tokens.hash_refresh_token(current_refresh_token)
            if current_refresh_token is not None
            else None
        )
        async with self.uow:
            await self.uow.refresh_tokens.revoke_all_for_user_except(
                user_id,
                keep_token_hash=keep_hash,
                keep_session_id=keep_session_id,
                revoked_at=self.clock.now(),
                reason=REVOKED_USER,
            )
            await self.uow.commit()
