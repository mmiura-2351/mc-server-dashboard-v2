"""Role + resource-grant evaluator: the PermissionChecker adapter (FR-AUTHZ-2).

The evaluator fetches the member's roles and the matching resource grant through
the community ``UnitOfWork`` and does the set math in Python. At M1 scale
(NFR-SCALE-1) a member holds a handful of roles and grants, so the union is
trivial; the Port shape (NFR-PORT-1) lets a smarter SQL adapter replace this
later without touching callers. :class:`RepositoryMembershipVisibility` is the
Layer-1 primitive over the same UnitOfWork.
"""

from __future__ import annotations

from mc_server_dashboard_api.community.domain.permission_checker import (
    MembershipVisibility,
    PermissionChecker,
)
from mc_server_dashboard_api.community.domain.permissions import (
    is_platform_admin_permission,
    require_known_permission,
)
from mc_server_dashboard_api.community.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    Permission,
    ResourceRef,
    UserId,
)


class RepositoryMembershipVisibility(MembershipVisibility):
    """:class:`MembershipVisibility` over the community ``UnitOfWork``."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def is_member(self, *, user_id: UserId, community_id: CommunityId) -> bool:
        async with self._uow as uow:
            membership = await uow.memberships.get_by_user_and_community(
                user_id, community_id
            )
        return membership is not None


class RoleGrantPermissionChecker(PermissionChecker):
    """:class:`PermissionChecker` evaluating roles ∪ resource grants (FR-AUTHZ-2)."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def can(
        self, *, user: AuthUser, operation: Permission, resource: ResourceRef
    ) -> bool:
        # Reject codes outside the authoritative catalog (FR-AUTHZ-3).
        require_known_permission(operation)

        # Platform-admin axis: decided on the flag, outside any Community context.
        if is_platform_admin_permission(operation):
            return user.is_platform_admin

        return operation in await self._effective_permissions(user.user_id, resource)

    async def _effective_permissions(
        self, user_id: UserId, resource: ResourceRef
    ) -> set[Permission]:
        async with self._uow as uow:
            membership = await uow.memberships.get_by_user_and_community(
                user_id, resource.community_id
            )
            if membership is None:
                return set()

            effective: set[Permission] = set()
            for role_id in await uow.memberships.list_role_ids(membership.id):
                role = await uow.roles.get_by_id(role_id)
                if role is not None:
                    effective |= role.permissions

            if resource.resource_type is not None and resource.resource_id is not None:
                grant = await uow.resource_grants.get_for_user_resource(
                    user_id,
                    resource.community_id,
                    resource.resource_type,
                    resource.resource_id,
                )
                if grant is not None:
                    effective |= grant.permissions

        return effective
