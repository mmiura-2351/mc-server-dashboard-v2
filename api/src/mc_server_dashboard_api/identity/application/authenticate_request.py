"""AuthenticateRequest use case: resolve the current user from an access token.

Backs the FastAPI auth dependency that every protected endpoint relies on. It
verifies the access token's signature/expiry via the :class:`TokenService` and
loads the referenced user. A bad token, or a token whose subject no longer
exists, raises :class:`InvalidAccessTokenError` — the edge maps that to 401.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import InvalidAccessTokenError
from mc_server_dashboard_api.identity.domain.token_service import TokenService
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class AuthenticateRequest:
    """Verify an access token and return its user."""

    uow: UnitOfWork
    tokens: TokenService

    async def __call__(self, *, access_token: str) -> User:
        user_id = self.tokens.verify_access_token(access_token)
        async with self.uow:
            user = await self.uow.users.get_by_id(user_id)
        if user is None:
            raise InvalidAccessTokenError
        return user
