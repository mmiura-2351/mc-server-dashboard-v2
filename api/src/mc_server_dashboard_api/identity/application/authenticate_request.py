"""AuthenticateRequest use case: resolve the current user from an access token.

Backs the FastAPI auth dependency that every protected endpoint relies on. It
verifies the access token's signature/expiry via the :class:`TokenService` and
loads the referenced user. A bad token, a token whose subject no longer exists,
or a token for a *deactivated* account raises :class:`InvalidAccessTokenError` —
the edge maps that to the uniform 401. The deactivation check here is what makes
an outstanding access token unusable the moment an admin deactivates the account
(issue #278): the user row is already loaded per request, so the flag check is
free, and the response is the same 401 a token-gone race returns (no oracle that
distinguishes a deactivated account from any other invalid token).
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
        if user is None or not user.active:
            raise InvalidAccessTokenError
        return user
