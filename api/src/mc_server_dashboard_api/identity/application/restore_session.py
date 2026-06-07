"""RestoreSession use case: turn a valid refresh token into an access token.

The Web UI bootstrap needs only "do I have a live session?" → a fresh access
token, which does *not* require rotating the refresh token. RefreshSession
rotates on every call, so any page load racing an in-flight rotation could leave
a revoked predecessor cookie in the jar; replaying it outside the reuse grace
revoked the whole token family and bounced the user to /login (issue #512). This
use case eliminates that race class: it validates the presented refresh secret
against the DATABASE.md validity rule (unrevoked AND unexpired) and issues a new
*access* token only. The refresh token is left exactly as it was — no rotation,
no new refresh row, no DB write.

A non-rotating restore does not weaken the theft model. Rotation and
reuse-detection still live entirely on :class:`RefreshSession` (the periodic
in-session refresh path), so a stolen refresh token is still invalidated the
moment the legitimate holder's next *refresh* rotates it. Restore never revokes
the family on a revoked/reused token — it has no rotation to disambiguate, so a
re-presented rotated predecessor is simply an invalid token here, not a theft
signal. An unknown, expired, or revoked token is rejected with
:class:`InvalidRefreshTokenError`, which the edge maps to the same uniform 401 as
every other auth failure.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.errors import InvalidRefreshTokenError
from mc_server_dashboard_api.identity.domain.token_service import TokenService
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class RestoreSession:
    """Validate a refresh token and mint a fresh access token without rotating."""

    uow: UnitOfWork
    tokens: TokenService
    clock: Clock

    async def __call__(self, *, refresh_token: str) -> str:
        token_hash = self.tokens.hash_refresh_token(refresh_token)
        now = self.clock.now()
        async with self.uow:
            stored = await self.uow.refresh_tokens.get_by_token_hash(token_hash)
        if stored is None or not stored.is_active(now=now):
            raise InvalidRefreshTokenError
        return self.tokens.issue_access_token(stored.user_id)
