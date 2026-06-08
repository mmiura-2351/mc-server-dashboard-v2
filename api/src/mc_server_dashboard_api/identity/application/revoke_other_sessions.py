"""RevokeOtherSessions use case: everywhere-else logout (issue #387).

Revokes all the caller's active refresh-token sessions except their current one,
stamping ``revoked_reason = 'user_revoked'``. The current session is identified by
the refresh token the caller presents (hashed and matched against the stored
rows): that one is spared, the rest revoked.

If the caller cannot present its current refresh token (``current_refresh_token``
is ``None`` -- e.g. the access-token-only Web UI path, whose cookie is confined to
``/api/auth`` and is not sent here), there is no row to spare: every active
session is revoked, including the current one. This is the safe choice -- it never
revokes *another* user's sessions, and a presented token is the only trustworthy
way to know which row is "current".
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.entities import REVOKED_USER
from mc_server_dashboard_api.identity.domain.token_service import TokenService
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import UserId


@dataclass(frozen=True)
class RevokeOtherSessions:
    """Revoke all the caller's sessions except the one they presented."""

    uow: UnitOfWork
    tokens: TokenService
    clock: Clock

    async def __call__(
        self, *, user_id: UserId, current_refresh_token: str | None
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
                revoked_at=self.clock.now(),
                reason=REVOKED_USER,
            )
            await self.uow.commit()
