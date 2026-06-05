"""DeleteAccount use case: the authenticated user deletes their own account.

Refuses (FR-COMM-4 / FR-AUTH-6) when the user still owns a community — deleting
would orphan it — or is the last platform administrator; both are domain errors
the edge maps to 409. Otherwise it deletes the user row and revokes their refresh
tokens atomically. The user's memberships, role assignments, and resource grants
are removed by the ``ON DELETE CASCADE`` foreign keys on ``user.id`` (DATABASE.md
Sections 4-6), and the refresh tokens would cascade too; they are revoked
explicitly first so the row is consistent even before the cascade and the intent
is auditable.

The ownership check goes through the :class:`CommunityOwnership` seam so the
identity context never imports the community context.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.community_ownership import (
    CommunityOwnership,
)
from mc_server_dashboard_api.identity.domain.errors import (
    CommunityOwnedError,
    LastPlatformAdminError,
    UserNotFoundError,
)
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import UserId


@dataclass(frozen=True)
class DeleteAccount:
    """Delete the authenticated user's account, guarding orphaned resources."""

    uow: UnitOfWork
    ownership: CommunityOwnership
    clock: Clock

    async def __call__(self, *, user_id: UserId) -> None:
        # NOTE: both guards below are check-then-act under READ COMMITTED with no
        # row lock, so concurrent self-deletes can race past them (last-admin and
        # community-owner TOCTOU). Closing it (FOR UPDATE / SERIALIZABLE / a DB
        # constraint) is deferred to #260.
        if await self.ownership.owns_any_community(user_id):
            raise CommunityOwnedError(str(user_id.value))

        async with self.uow:
            user = await self.uow.users.get_by_id(user_id)
            if user is None:
                raise UserNotFoundError(str(user_id.value))
            if (
                user.is_platform_admin
                and await self.uow.users.count_platform_admins() <= 1
            ):
                raise LastPlatformAdminError(str(user_id.value))

            await self.uow.refresh_tokens.revoke_all_for_user(
                user.id, revoked_at=self.clock.now()
            )
            await self.uow.users.delete(user.id)
            await self.uow.commit()
