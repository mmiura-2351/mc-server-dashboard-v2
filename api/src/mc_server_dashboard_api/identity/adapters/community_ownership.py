"""Adapter implementing the identity :class:`CommunityOwnership` Port over community.

The edge where an account deletion asks "does this user own any community?"
(FR-COMM-4). Only the *adapter* crosses contexts — the identity domain stays
unaware of how communities model ownership — and it asks only whether the user
holds the preset Owner role in any community they are a member of, the same
definition the community context's last-Owner guard uses.

It runs on the identity unit of work's session, inside the delete transaction,
under the user's community-ownership lock (``core/adapters/ownership_lock.py``,
#3217).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from mc_server_dashboard_api.community.adapters.repositories import (
    SqlAlchemyMembershipRepository,
    SqlAlchemyRoleRepository,
)
from mc_server_dashboard_api.community.domain.permissions import OWNER_ROLE_NAME
from mc_server_dashboard_api.community.domain.value_objects import RoleName
from mc_server_dashboard_api.community.domain.value_objects import (
    UserId as CommunityUserId,
)
from mc_server_dashboard_api.core.adapters.ownership_lock import (
    lock_community_ownership,
)
from mc_server_dashboard_api.identity.domain.community_ownership import (
    CommunityOwnership,
)
from mc_server_dashboard_api.identity.domain.value_objects import UserId


class CommunityBackedOwnership(CommunityOwnership):
    """:class:`CommunityOwnership` backed by the community store on one session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._memberships = SqlAlchemyMembershipRepository(session)
        self._roles = SqlAlchemyRoleRepository(session)

    async def owns_any_community(self, user_id: UserId) -> bool:
        # Exclusive: a conferral on this user holds the lock shared until it
        # commits, so this waits for one in flight, and the reads below -- each
        # a new snapshot -- see the ownership it committed.
        await lock_community_ownership(self._session, user_id.value, shared=False)
        community_user_id = CommunityUserId(user_id.value)
        for membership in await self._memberships.list_for_user(community_user_id):
            owner_role = next(
                (
                    role
                    for role in await self._roles.list_for_community(
                        membership.community_id
                    )
                    if role.is_preset and role.name == RoleName(OWNER_ROLE_NAME)
                ),
                None,
            )
            if owner_role is None:
                continue
            held = await self._memberships.list_role_ids(membership.id)
            if owner_role.id in held:
                return True
        return False
