"""Logout use case: end the presented refresh token's session (FR-AUTH-3).

Hashes the presented secret and revokes the matching token's whole rotation
chain -- the sign-in session it belongs to -- so no token of that session can be
refreshed any more. Revoking only the presented token left a successor valid when
a refresh had rotated it but its response was still in flight: the late
``Set-Cookie`` then installed a live session in a browser that had logged out,
over the next user's cookie (issue #3249). The owner's session lock serializes
logout with a rotation, so a successor still being minted is revoked too. The
user's other sessions (other chains) are untouched.

Logout is idempotent and does not leak whether the token existed: an unknown or
already-revoked token is accepted silently (no enumeration signal). Access tokens
are short-lived and not persisted, so nothing else needs revoking here.
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
        superseded_hash = (
            self.tokens.hash_refresh_token(superseded_token)
            if superseded_token is not None and superseded_token != refresh_token
            else None
        )
        locked_hashes = [token_hash]
        if superseded_hash is not None:
            locked_hashes.append(superseded_hash)
        async with self.uow:
            locked = await self.uow.refresh_tokens.lock_sessions_by_token_hashes(
                locked_hashes
            )
            stored = locked.get(token_hash)
            if stored is not None:
                await self.uow.refresh_tokens.revoke_chain(
                    stored.chain_id, revoked_at=now, reason=REVOKED_LOGOUT
                )
            # Both-transports logout: the body token wins, but the cookie-carried
            # token's session must end too, otherwise it stays valid server-side
            # while the browser jar already overwrote it -- a dangling session no
            # client holds (issue #384). Its whole chain is revoked, so a
            # successor that a rotation of the cookie is minting dies with it
            # (issue #3251). An unknown cookie revokes nothing, and a cookie of
            # the body token's own chain finds it already revoked above.
            superseded = (
                locked.get(superseded_hash) if superseded_hash is not None else None
            )
            if superseded is not None:
                await self.uow.refresh_tokens.revoke_chain(
                    superseded.chain_id, revoked_at=now, reason=REVOKED_SUPERSEDED
                )
            await self.uow.commit()
