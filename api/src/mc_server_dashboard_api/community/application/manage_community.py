"""Read / rename / delete use cases for an existing community (FR-COMM-1/3).

These run *after* the route's two-layer authorization dependency has admitted the
caller (non-member -> 404, member-without-permission -> 403; Section 6.4), so they
assume an authorized member and only do the data work. ``RenameCommunity`` is the
M1 "update" — only the name is mutable; the quota fields stay unwritten (decision
#9). ``DeleteCommunity`` removes the row and the database cascades to every
dependent (DATABASE.md Section 10).
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.community.domain.clock import Clock
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.errors import (
    CommunityAlreadyExistsError,
    CommunityNotFoundError,
)
from mc_server_dashboard_api.community.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    CommunityName,
)


@dataclass(frozen=True)
class ReadCommunity:
    """Return an existing community by id."""

    uow: UnitOfWork

    async def __call__(self, *, community_id: CommunityId) -> Community:
        async with self.uow:
            community = await self.uow.communities.get_by_id(community_id)
        if community is None:
            raise CommunityNotFoundError(str(community_id.value))
        return community


@dataclass(frozen=True)
class RenameCommunity:
    """Rename an existing community (the only mutable field in M1)."""

    uow: UnitOfWork
    clock: Clock

    async def __call__(self, *, community_id: CommunityId, name: str) -> Community:
        new_name = CommunityName(name)
        async with self.uow:
            community = await self.uow.communities.get_by_id(community_id)
            if community is None:
                raise CommunityNotFoundError(str(community_id.value))
            existing = await self.uow.communities.get_by_name(new_name)
            if existing is not None and existing.id != community_id:
                raise CommunityAlreadyExistsError(new_name.value)
            community.name = new_name
            community.updated_at = self.clock.now()
            await self.uow.communities.update(community)
            await self.uow.commit()
        return community


@dataclass(frozen=True)
class DeleteCommunity:
    """Delete an existing community, cascading to its dependents (Section 10)."""

    uow: UnitOfWork

    async def __call__(self, *, community_id: CommunityId) -> None:
        async with self.uow:
            community = await self.uow.communities.get_by_id(community_id)
            if community is None:
                raise CommunityNotFoundError(str(community_id.value))
            await self.uow.communities.delete(community_id)
            await self.uow.commit()
