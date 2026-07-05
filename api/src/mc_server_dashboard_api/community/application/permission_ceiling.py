"""Grant-only-what-you-hold ceiling enforcement (issue #1595).

When a role or grant is created/updated, the permissions being conferred must
be a subset of the actor's own effective permissions in the community.  This
prevents a ``role:manage`` or ``grant:manage`` holder from escalating to
owner-equivalent control by conferring permissions they do not possess.
"""

from __future__ import annotations

import uuid

from mc_server_dashboard_api.community.domain.errors import (
    PermissionCeilingExceededError,
)
from mc_server_dashboard_api.community.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    Permission,
    UserId,
)


async def enforce_permission_ceiling(
    uow: UnitOfWork,
    *,
    actor_id: UserId,
    community_id: CommunityId,
    conferred: set[Permission],
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
) -> None:
    """Raise if ``conferred`` contains permissions the actor lacks.

    The actor's ceiling is the union of all permissions from their roles in the
    community.  When ``resource_type`` and ``resource_id`` are given (grant
    operations), the actor's own grant on that exact resource also counts toward
    their ceiling.

    Raises :class:`PermissionCeilingExceededError` listing the exceeded codes.
    """
    if not conferred:
        return

    # Build the actor's ceiling from their role permissions.
    membership = await uow.memberships.get_by_user_and_community(actor_id, community_id)
    ceiling: set[Permission] = set()
    if membership is not None:
        role_ids = await uow.memberships.list_role_ids(membership.id)
        if role_ids:
            roles = await uow.roles.get_by_ids(role_ids)
            for role in roles:
                ceiling |= role.permissions

    # For grant operations, the actor's own grant on the same resource counts.
    if resource_type is not None and resource_id is not None:
        actor_grant = await uow.resource_grants.get_for_user_resource(
            actor_id, community_id, resource_type, resource_id
        )
        if actor_grant is not None:
            ceiling |= actor_grant.permissions

    exceeded = conferred - ceiling
    if exceeded:
        raise PermissionCeilingExceededError(sorted(p.value for p in exceeded))
