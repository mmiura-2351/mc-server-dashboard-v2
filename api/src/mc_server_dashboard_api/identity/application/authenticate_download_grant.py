"""AuthenticateDownloadGrant use case: resolve the subject of a download grant.

The redemption-side twin of :class:`AuthenticateRequest` (issue #2313). It
verifies the grant against the resource being fetched via the
:class:`TokenService` and loads the referenced user, so a grant whose subject was
deleted or deactivated between issuance and redemption is rejected rather than
honoured for its full TTL.

The grant proves *identity* only. Authority is decided afresh at the edge on
every redemption, so a permission revocation or a membership removal takes effect
immediately even on an already-minted URL.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import InvalidDownloadGrantError
from mc_server_dashboard_api.identity.domain.token_service import TokenService
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class AuthenticateDownloadGrant:
    """Verify a download grant for ``resource`` and return its user."""

    uow: UnitOfWork
    tokens: TokenService

    async def __call__(self, *, grant: str, resource: str) -> User:
        user_id = self.tokens.verify_download_grant(grant, resource)
        async with self.uow:
            user = await self.uow.users.get_by_id(user_id)
        if user is None or not user.active:
            raise InvalidDownloadGrantError
        return user
