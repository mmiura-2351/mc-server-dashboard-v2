"""Resource-grant use cases: list / create / revoke per-resource grants (6.4).

These run *after* the route's two-layer authorization dependency has admitted the
caller (non-member -> 404, member-without-permission -> 403; Section 6.4), so they
assume an authorized member and only do the data work.

- :class:`ListGrants` returns the community's grants, optionally filtered to one
  member (grant:read).
- :class:`CreateGrant` grants a permission set to a member on a specific resource
  (FR-AUTHZ-2). It validates that the target user is a *member* of the community,
  that ``resource_type`` is a known M1 type (``server``; the CHECK-constrained enum,
  DATABASE.md Section 6), that every permission is valid for that resource type
  (server / file / backup families), and that the resource *exists* in the
  community — a fabricated ``resource_id`` is rejected with
  :class:`GrantResourceNotFoundError` rather than persisted as a ghost grant
  (issue #361). A duplicate ``(user, resource_type, resource_id)`` surfaces as
  :class:`ResourceGrantAlreadyExistsError` (the unique constraint, translated by
  the UnitOfWork).
- :class:`RevokeGrant` deletes a grant by id, scoped to this community so a caller
  cannot probe another community's grant ids (FR-AUTHZ-4): a mismatch is reported
  as not-found.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.community.domain.clock import Clock
from mc_server_dashboard_api.community.domain.entities import ResourceGrant
from mc_server_dashboard_api.community.domain.errors import (
    GrantResourceNotFoundError,
    GrantTargetNotMemberError,
    InvalidGrantResourceTypeError,
    ResourceGrantNotFoundError,
)
from mc_server_dashboard_api.community.domain.permissions import (
    GRANT_PERMISSIONS_BY_RESOURCE_TYPE,
    require_grant_permission,
)
from mc_server_dashboard_api.community.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    Permission,
    ResourceGrantId,
    UserId,
)


@dataclass(frozen=True)
class ListGrants:
    """List the community's resource grants, optionally per member (grant:read)."""

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, user_id: UserId | None = None
    ) -> list[ResourceGrant]:
        async with self.uow:
            return await self.uow.resource_grants.list_for_community(
                community_id, user_id
            )


@dataclass(frozen=True)
class CreateGrant:
    """Grant a permission set to a member on a specific resource (grant:manage)."""

    uow: UnitOfWork
    clock: Clock

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        user_id: UserId,
        resource_type: str,
        resource_id: uuid.UUID,
        permissions: set[Permission],
    ) -> ResourceGrant:
        if resource_type not in GRANT_PERMISSIONS_BY_RESOURCE_TYPE:
            raise InvalidGrantResourceTypeError(resource_type)
        validated = {
            require_grant_permission(perm, resource_type=resource_type)
            for perm in permissions
        }

        now = self.clock.now()
        grant = ResourceGrant(
            id=ResourceGrantId.new(),
            user_id=user_id,
            community_id=community_id,
            resource_type=resource_type,
            resource_id=resource_id,
            permissions=validated,
            created_at=now,
            updated_at=now,
        )
        async with self.uow:
            membership = await self.uow.memberships.get_by_user_and_community(
                user_id, community_id
            )
            if membership is None:
                raise GrantTargetNotMemberError(str(user_id.value))
            if not await self.uow.resources.exists(
                community_id, resource_type, resource_id
            ):
                raise GrantResourceNotFoundError(str(resource_id))
            await self.uow.resource_grants.add(grant)
            await self.uow.commit()
        return grant


@dataclass(frozen=True)
class RevokeGrant:
    """Revoke a resource grant by id, scoped to this community (grant:manage)."""

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, grant_id: ResourceGrantId
    ) -> None:
        async with self.uow:
            grant = await self.uow.resource_grants.get_by_id(grant_id)
            if grant is None or grant.community_id != community_id:
                raise ResourceGrantNotFoundError(str(grant_id.value))
            await self.uow.resource_grants.delete(grant_id)
            await self.uow.commit()
