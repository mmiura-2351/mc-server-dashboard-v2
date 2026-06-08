"""Logout use case: revoke the presented refresh token (FR-AUTH-3).

Hashes the presented secret and revokes the matching row so the session can no
longer be refreshed. Logout is idempotent and does not leak whether the token
existed: an unknown or already-revoked token is accepted silently (no enumeration
signal). Access tokens are short-lived and not persisted, so nothing else needs
revoking here.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.entities import (
    REVOKED_LOGOUT,
    REVOKED_SUPERSEDED,
)
from mc_server_dashboard_api.identity.domain.token_service import TokenService
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class Logout:
    """Revoke a refresh token, ending its session."""

    uow: UnitOfWork
    tokens: TokenService
    clock: Clock

    async def __call__(
        self, *, refresh_token: str, superseded_token: str | None = None
    ) -> None:
        now = self.clock.now()
        token_hash = self.tokens.hash_refresh_token(refresh_token)
        async with self.uow:
            await self.uow.refresh_tokens.revoke(
                token_hash, revoked_at=now, reason=REVOKED_LOGOUT
            )
            # Both-transports logout: the body token wins, but the cookie-carried
            # token must be revoked too, otherwise it stays valid server-side while
            # the browser jar already overwrote it -- a dangling session no client
            # holds (issue #384). Revoke is idempotent on a missing/already-revoked
            # row, so a different-but-already-dead or unknown cookie is a no-op.
            if superseded_token is not None:
                superseded_hash = self.tokens.hash_refresh_token(superseded_token)
                if superseded_hash != token_hash:
                    await self.uow.refresh_tokens.revoke(
                        superseded_hash, revoked_at=now, reason=REVOKED_SUPERSEDED
                    )
            await self.uow.commit()
