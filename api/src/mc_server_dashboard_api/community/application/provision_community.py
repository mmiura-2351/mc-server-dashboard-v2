"""ProvisionCommunity use case: platform-admin community creation (FR-COMM-2).

Creates a community and bootstraps its ownership in one transaction (FR-COMM-4):
the community row, a preset ``Owner`` role granting every community-scoped
permission, the initial owner's membership, and that membership's assignment of
the Owner role. The owner is validated against the :class:`UserDirectory` Port
first (so an unknown user fails cleanly rather than tripping the FK), and the
Owner role's permission set is derived from the authoritative catalog
(:data:`COMMUNITY_PERMISSIONS`) — never hand-written.

The whole bootstrap commits atomically: if any step fails, the ``UnitOfWork``
rolls back and no partial community is left behind.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.community.domain.clock import Clock
from mc_server_dashboard_api.community.domain.entities import (
    Community,
    Membership,
    Role,
)
from mc_server_dashboard_api.community.domain.errors import (
    CommunityAlreadyExistsError,
    OwnerUserNotFoundError,
)
from mc_server_dashboard_api.community.domain.permissions import (
    COMMUNITY_PERMISSIONS,
    OWNER_ROLE_NAME,
)
from mc_server_dashboard_api.community.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.community.domain.user_directory import UserDirectory
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    CommunityName,
    MembershipId,
    RoleId,
    RoleName,
    UserId,
)


@dataclass(frozen=True)
class ProvisionCommunity:
    """Provision a community with its Owner role and initial owner membership."""

    uow: UnitOfWork
    users: UserDirectory
    clock: Clock

    async def __call__(self, *, name: str, owner_user_id: UserId) -> Community:
        community_name = CommunityName(name)

        if not await self.users.exists(owner_user_id):
            raise OwnerUserNotFoundError(str(owner_user_id.value))

        now = self.clock.now()
        community = Community(
            id=CommunityId.new(),
            name=community_name,
            created_at=now,
            updated_at=now,
        )
        owner_role = Role(
            id=RoleId.new(),
            community_id=community.id,
            name=RoleName(OWNER_ROLE_NAME),
            permissions=set(COMMUNITY_PERMISSIONS),
            created_at=now,
            updated_at=now,
            is_preset=True,
        )
        membership = Membership(
            id=MembershipId.new(),
            user_id=owner_user_id,
            community_id=community.id,
            created_at=now,
        )

        async with self.uow:
            if await self.uow.communities.get_by_name(community_name) is not None:
                raise CommunityAlreadyExistsError(community_name.value)
            await self.uow.communities.add(community)
            await self.uow.roles.add(owner_role)
            await self.uow.memberships.add(membership)
            # Flush so the role/membership rows exist before the membership_role
            # row that FKs them is staged (the join has no ORM relationship to
            # order the inserts). Still one transaction — commit makes it durable.
            await self.uow.flush()
            await self.uow.memberships.assign_role(membership.id, owner_role.id)
            await self.uow.commit()
        return community
