"""RefreshSession use case: rotate a refresh token for a new pair (FR-AUTH-2).

Validates the presented refresh secret against the stored hash, then *rotates*:
the old token is revoked and a fresh access + refresh pair is issued, all in one
transaction (DATABASE.md Section 4 validity rule; rotation atomicity per the
issue). An unknown, expired, or already-revoked token is rejected with
:class:`InvalidRefreshTokenError`.

**Reuse policy.** DATABASE.md and SECURITY.md are silent on what to do when an
*already-revoked* (i.e. previously rotated) token is presented again. Presenting
a rotated token means either a replay or a leaked secret, so this use case treats
it conservatively: it revokes *all* of that user's still-active tokens (the token
family) and rejects. A stolen refresh token thus cannot outlive the legitimate
holder's next refresh.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from mc_server_dashboard_api.identity.application.issue_tokens import issue_token_pair
from mc_server_dashboard_api.identity.application.token_pair import TokenPair
from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.errors import InvalidRefreshTokenError
from mc_server_dashboard_api.identity.domain.token_service import TokenService
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class RefreshSession:
    """Rotate a valid refresh token into a new access + refresh pair."""

    uow: UnitOfWork
    tokens: TokenService
    clock: Clock
    refresh_ttl: dt.timedelta

    async def __call__(self, *, refresh_token: str) -> TokenPair:
        token_hash = self.tokens.hash_refresh_token(refresh_token)
        now = self.clock.now()
        async with self.uow:
            stored = await self.uow.refresh_tokens.get_by_token_hash(token_hash)
            if stored is None or stored.expires_at <= now:
                raise InvalidRefreshTokenError
            if stored.revoked_at is not None:
                # Reuse of an already-rotated token: revoke the whole family.
                await self.uow.refresh_tokens.revoke_all_for_user(
                    stored.user_id, revoked_at=now
                )
                await self.uow.commit()
                raise InvalidRefreshTokenError

            await self.uow.refresh_tokens.revoke(token_hash, revoked_at=now)
            pair = await issue_token_pair(
                uow=self.uow,
                tokens=self.tokens,
                user_id=stored.user_id,
                now=now,
                refresh_ttl=self.refresh_ttl,
            )
            await self.uow.commit()
        return pair
