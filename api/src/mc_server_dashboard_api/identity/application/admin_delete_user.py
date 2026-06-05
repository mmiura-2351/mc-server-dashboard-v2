"""AdminDeleteUser use case: a platform admin deletes another user's account.

Mirrors the self-service :class:`DeleteAccount` refusals (issue #258) -- a
community owner or the last active platform admin cannot be deleted -- and adds
the admin-route-specific guard that an admin cannot delete *themselves* through
this route (they must use ``DELETE /users/me``). Otherwise it deletes the user
row and revokes their refresh tokens; memberships, role assignments, and resource
grants are removed by the ``ON DELETE CASCADE`` foreign keys on ``user.id``.

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
    SelfTargetError,
    UserNotFoundError,
)
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import UserId


@dataclass(frozen=True)
class AdminDeleteUser:
    """Delete a target user's account on behalf of a platform admin."""

    uow: UnitOfWork
    ownership: CommunityOwnership
    clock: Clock

    async def __call__(self, *, actor_id: UserId, target_id: UserId) -> None:
        # The community-owner check still runs in its own committed transaction
        # (a separate context's seam), so its TOCTOU window is unchanged; #260
        # closes only the last-active-admin race below.
        if target_id == actor_id:
            raise SelfTargetError(str(target_id.value))

        if await self.ownership.owns_any_community(target_id):
            raise CommunityOwnedError(str(target_id.value))

        async with self.uow:
            user = await self.uow.users.get_by_id(target_id)
            if user is None:
                raise UserNotFoundError(str(target_id.value))
            # Deleting an active admin reduces the set, so take a FOR UPDATE lock
            # so concurrent last-two-admin deletes serialize and exactly one wins
            # (#260); deleting a non-admin or inactive user stays lock-free.
            if (
                user.is_platform_admin
                and user.active
                and await self.uow.users.lock_active_platform_admins() <= 1
            ):
                raise LastPlatformAdminError(str(target_id.value))

            await self.uow.refresh_tokens.revoke_all_for_user(
                user.id, revoked_at=self.clock.now()
            )
            await self.uow.users.delete(user.id)
            await self.uow.commit()
