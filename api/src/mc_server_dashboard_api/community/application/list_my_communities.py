"""ListMyCommunities use case: the requesting user's communities (FR-MEM-4).

A user's view is always scoped to the communities they are a member of. This
walks the user's memberships and returns the corresponding communities; it needs
no authorization gate beyond authentication, because it only ever exposes
communities the user already belongs to (Layer-1 visibility is satisfied by
construction).
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.community.domain.value_objects import UserId


@dataclass(frozen=True)
class ListMyCommunities:
    """List the communities the requesting user is a member of."""

    uow: UnitOfWork

    async def __call__(self, *, user_id: UserId) -> list[Community]:
        async with self.uow:
            memberships = await self.uow.memberships.list_for_user(user_id)
            communities = []
            for membership in memberships:
                community = await self.uow.communities.get_by_id(
                    membership.community_id
                )
                if community is not None:
                    communities.append(community)
        return communities
