"""Adapter implementing the identity :class:`CommunityOwnership` Port over community.

The edge where identity's self-delete guard asks "does this user own any
community?" (FR-COMM-4). Only the *adapter* crosses contexts — the identity
domain stays unaware of how communities model ownership — and it asks only
whether the user holds the preset Owner role in any community they are a member
of, the same definition the community context's last-Owner guard uses.
"""

from __future__ import annotations

from mc_server_dashboard_api.community.domain.permissions import OWNER_ROLE_NAME
from mc_server_dashboard_api.community.domain.unit_of_work import (
    UnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.domain.value_objects import RoleName
from mc_server_dashboard_api.community.domain.value_objects import (
    UserId as CommunityUserId,
)
from mc_server_dashboard_api.identity.domain.community_ownership import (
    CommunityOwnership,
)
from mc_server_dashboard_api.identity.domain.value_objects import UserId


class CommunityBackedOwnership(CommunityOwnership):
    """:class:`CommunityOwnership` backed by the community store."""

    def __init__(self, uow: CommunityUnitOfWork) -> None:
        self._uow = uow

    async def owns_any_community(self, user_id: UserId) -> bool:
        community_user_id = CommunityUserId(user_id.value)
        async with self._uow as uow:
            memberships = await uow.memberships.list_for_user(community_user_id)
            for membership in memberships:
                owner_role = next(
                    (
                        role
                        for role in await uow.roles.list_for_community(
                            membership.community_id
                        )
                        if role.is_preset and role.name == RoleName(OWNER_ROLE_NAME)
                    ),
                    None,
                )
                if owner_role is None:
                    continue
                held = await uow.memberships.list_role_ids(membership.id)
                if owner_role.id in held:
                    return True
        return False
