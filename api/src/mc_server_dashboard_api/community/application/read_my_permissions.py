"""Read the caller's own effective permission set in a community (issue #354).

The UI needs to know what the caller may do to scope its controls; server-side
enforcement stays authoritative (FR-AUTHZ-6), this is rendering convenience only.

:class:`ReadMyEffectivePermissions` runs *after* the route's Layer-1 visibility
check has admitted the caller (non-member -> 404, no existence signal; Section
6.4), so it assumes a member and only does the data work. It reads the SAME
stores the ``PermissionChecker`` reads — the union of the member's role
permission sets and the caller's own resource grants (FR-AUTHZ-2) — so the
introspected set cannot drift from what enforcement actually allows. A platform
admin gets the full community catalog, mirroring the checker's admin bypass
(FR-AUTHZ-5).
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.community.domain.entities import ResourceGrant
from mc_server_dashboard_api.community.domain.permissions import COMMUNITY_PERMISSIONS
from mc_server_dashboard_api.community.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    Permission,
)


@dataclass(frozen=True)
class EffectivePermissions:
    """The caller's community-wide permission codes and per-resource grants."""

    permissions: set[Permission]
    grants: list[ResourceGrant]


@dataclass(frozen=True)
class ReadMyEffectivePermissions:
    """Return the caller's own effective permission set in a community."""

    uow: UnitOfWork

    async def __call__(
        self, *, user: AuthUser, community_id: CommunityId
    ) -> EffectivePermissions:
        async with self.uow:
            grants = await self.uow.resource_grants.list_for_community(
                community_id, user.user_id
            )
            # A platform admin's community-wide set is the full catalog, mirroring
            # the checker's admin bypass (FR-AUTHZ-5); their own grants are still
            # surfaced as-is.
            if user.is_platform_admin:
                return EffectivePermissions(
                    permissions=set(COMMUNITY_PERMISSIONS), grants=grants
                )

            membership = await self.uow.memberships.get_by_user_and_community(
                user.user_id, community_id
            )
            permissions: set[Permission] = set()
            if membership is not None:
                role_ids = await self.uow.memberships.list_role_ids(membership.id)
                for role in await self.uow.roles.get_by_ids(role_ids):
                    permissions |= role.permissions

        return EffectivePermissions(permissions=permissions, grants=grants)
