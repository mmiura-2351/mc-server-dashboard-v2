"""RefreshSession use case: rotate a refresh token for a new pair (FR-AUTH-2).

Validates the presented refresh secret against the stored hash, then *rotates*:
the old token is revoked and a fresh access + refresh pair is issued, all in one
transaction (DATABASE.md Section 4 validity rule; rotation atomicity per the
issue). An unknown, expired, or already-revoked token is rejected with
:class:`InvalidRefreshTokenError`.

**Reuse policy.** DATABASE.md and SECURITY.md are silent on what to do when an
*already-revoked* (i.e. previously rotated) token is presented again. Presenting
a rotated token is ambiguous: it may be a legitimate concurrent refresh (two SPA
tabs, or a client retrying a refresh whose response was lost after the server
committed the rotation — issue #369), or a replay of a leaked secret. To
disambiguate, this use case uses a short *reuse grace window* (``reuse_grace``):
within that window of the predecessor's revocation the reuse is treated as a
legitimate concurrent refresh and rotated normally (a fresh pair is issued, the
token family is left intact). The server stores only token hashes, so the
successor secret cannot be replayed back; a fresh pair per grace-window reuse is
the accepted design and replay exposure is bounded by the window. Outside the
window the reuse is treated as theft: *all* of that user's still-active tokens
(the token family) are revoked and the request rejected, so a stolen refresh
token cannot outlive the legitimate holder's next refresh.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from mc_server_dashboard_api.identity.application.issue_tokens import issue_token_pair
from mc_server_dashboard_api.identity.application.token_pair import TokenPair
from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.errors import (
    InvalidRefreshTokenError,
    RefreshTokenReuseError,
)
from mc_server_dashboard_api.identity.domain.token_service import TokenService
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class RefreshSession:
    """Rotate a valid refresh token into a new access + refresh pair."""

    uow: UnitOfWork
    tokens: TokenService
    clock: Clock
    refresh_ttl: dt.timedelta
    reuse_grace: dt.timedelta

    async def __call__(self, *, refresh_token: str) -> TokenPair:
        token_hash = self.tokens.hash_refresh_token(refresh_token)
        now = self.clock.now()
        async with self.uow:
            stored = await self.uow.refresh_tokens.get_by_token_hash(token_hash)
            if stored is None or stored.expires_at <= now:
                raise InvalidRefreshTokenError
            if (
                stored.revoked_at is not None
                and now - stored.revoked_at > self.reuse_grace
            ):
                # Reuse outside the grace window: treat as theft and revoke the
                # whole family.
                await self.uow.refresh_tokens.revoke_all_for_user(
                    stored.user_id, revoked_at=now
                )
                await self.uow.commit()
                # Distinguishable from a plain bad token so the route can record
                # the family-revocation as a DENIED security event (FR-AUD-1),
                # attributed to the affected user.
                raise RefreshTokenReuseError(stored.user_id.value)

            # A still-active token, or a reuse within the grace window (a
            # legitimate concurrent refresh / lost-response retry, issue #369):
            # rotate normally. Only an active token needs revoking; re-revoking a
            # grace-window predecessor would push its revocation time forward and
            # roll the window, letting repeated reuse keep a leaked token alive.
            if stored.revoked_at is None:
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
