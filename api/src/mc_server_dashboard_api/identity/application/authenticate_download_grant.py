"""AuthenticateDownloadGrant use case: resolve the subject of a download grant.

The redemption-side twin of :class:`AuthenticateRequest` (issue #2313). It
verifies the grant against the resource being fetched via the
:class:`TokenService` and loads the referenced user, so a grant whose subject was
deleted or deactivated between issuance and redemption is rejected rather than
honoured for its full TTL.

The grant travels in two transports, and this use case authenticates both: the
``?grant=`` query parameter, and the ``HttpOnly`` cookie a redemption mints so an
interrupted transfer can be retried after the query-string window has closed
(issue #2373). Both carry the same resource-scoped authority, and each is verified
only in the transport it was minted for.

Neither proves *authority*. That is decided afresh at the edge on every
redemption, so a permission revocation or a membership removal takes effect
immediately even on an already-minted URL or an already-issued cookie.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import (
    InvalidDownloadCookieError,
    InvalidDownloadGrantError,
)
from mc_server_dashboard_api.identity.domain.token_service import TokenService
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import UserId


@dataclass(frozen=True)
class AuthenticateDownloadGrant:
    """Verify a download grant for ``resource`` and return its user.

    ``__call__`` takes the query-string transport, :meth:`from_cookie` the cookie
    one; both bind to ``resource`` and neither accepts the other's token.
    """

    uow: UnitOfWork
    tokens: TokenService

    async def __call__(self, *, grant: str, resource: str) -> User:
        user_id = self.tokens.verify_download_grant(grant, resource)
        user = await self._active_user(user_id)
        if user is None:
            raise InvalidDownloadGrantError
        return user

    async def from_cookie(self, *, cookie: str, resource: str) -> User:
        """The same, for the cookie transport a redemption minted (issue #2373)."""

        user_id = self.tokens.verify_download_cookie(cookie, resource)
        user = await self._active_user(user_id)
        if user is None:
            raise InvalidDownloadCookieError
        return user

    async def _active_user(self, user_id: UserId) -> User | None:
        """The named user if it still exists and is active, else ``None``.

        Loading the row is what makes a credential whose subject was deleted or
        deactivated after issuance fail closed rather than run for its full TTL.
        """

        async with self.uow:
            user = await self.uow.users.get_by_id(user_id)
        return user if user is not None and user.active else None
