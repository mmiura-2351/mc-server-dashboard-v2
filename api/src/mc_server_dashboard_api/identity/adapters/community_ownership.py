"""Adapter implementing the identity :class:`CommunityOwnership` Port over community.

The edge where an account deletion asks "does this user own any community?"
(FR-COMM-4). Only the *adapter* crosses contexts — the identity domain stays
unaware of how communities model ownership — and it asks only whether the user
holds the preset Owner role in any community they are a member of, the same
definition the community context's last-Owner guard uses.

It runs on the identity unit of work's session, inside the delete transaction,
under the user's community-ownership lock (``core/adapters/ownership_lock.py``,
#3217). A deletion reads this while it holds every active admin's ``user`` row,
so the answer is one statement over the community tables, however many
memberships the user has.
"""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from mc_server_dashboard_api.community.adapters.models import (
    MembershipModel,
    MembershipRoleModel,
    RoleModel,
)
from mc_server_dashboard_api.community.domain.permissions import OWNER_ROLE_NAME
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

    async def owns_any_community(self, user_id: UserId) -> bool:
        # Exclusive: a conferral on this user holds the lock shared until it
        # commits, so this waits for one in flight, and the read below -- a new
        # snapshot -- sees the ownership it committed.
        await lock_community_ownership(self._session, user_id.value, shared=False)
        # One of the user's memberships holds its own community's preset Owner.
        stmt = select(
            exists().where(
                MembershipModel.user_id == user_id.value,
                MembershipRoleModel.membership_id == MembershipModel.id,
                RoleModel.id == MembershipRoleModel.role_id,
                RoleModel.community_id == MembershipModel.community_id,
                RoleModel.is_preset.is_(True),
                RoleModel.name == OWNER_ROLE_NAME,
            )
        )
        return bool((await self._session.execute(stmt)).scalar_one())
