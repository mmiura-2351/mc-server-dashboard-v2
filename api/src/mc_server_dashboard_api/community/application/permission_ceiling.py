"""Grant-only-what-you-hold ceiling enforcement (issue #1595).

When a role or grant is created/updated, the permissions being conferred must
be a subset of the actor's own effective permissions in the community.  This
prevents a ``role:manage`` or ``grant:manage`` holder from escalating to
owner-equivalent control by conferring permissions they do not possess.

The ceiling is read under lock (#3241): the rows it is computed from -- the
actor's roles, their assignments of those roles, and their grant on the
resource -- stay share-locked until the conferring write commits. A revocation
of any of them (a permission removed from the role, the role unassigned or
deleted, the member removed, the grant revoked) therefore either commits first,
and the ceiling sees it, or waits until the conferral has committed; a conferral
never commits after the authority it relied on was revoked.

Lock order, the same for every transaction that locks through here:

1. the grant's resource (``FOR KEY SHARE``, grant creation's existence check);
2. role rows, one at a time in ascending id order (``FOR SHARE``; the role an
   update replaces the permissions of ``FOR UPDATE``, in its place in the order);
3. the actor's ``membership_role`` rows (``FOR SHARE``);
4. the actor's grant on the resource (``FOR SHARE``).

Each row is taken at its final strength in one pass, so two actors editing each
other's roles cannot deadlock. Roles precede their assignments and the resource
precedes its grants because that is the order the cascades of a role deletion
and a server deletion lock them in; assignments precede the grant as a member
removal's cascade does.
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
    RoleId,
    UserId,
)


async def lock_actor_ceiling(
    uow: UnitOfWork,
    *,
    actor_id: UserId,
    community_id: CommunityId,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    role_to_update: RoleId | None = None,
) -> set[Permission]:
    """Return the actor's ceiling, its sources locked until the transaction ends.

    The ceiling is the union of the permissions of the actor's roles in the
    community. When ``resource_type`` and ``resource_id`` are given (grant
    operations), the actor's own grant on that exact resource also counts.

    ``role_to_update`` is locked ``FOR UPDATE`` in the same ordered pass as the
    actor's roles, for a role update whose write depends on that role's current
    state; it counts toward the ceiling only if the actor holds it.
    """
    membership = await uow.memberships.get_by_user_and_community(actor_id, community_id)
    role_ids = (
        [] if membership is None else await uow.memberships.list_role_ids(membership.id)
    )
    roles = await uow.roles.lock_by_ids(role_ids, for_update=role_to_update)
    ceiling: set[Permission] = set()
    if membership is not None:
        # Locked after the roles, as a role deletion's cascade locks them. An
        # assignment revoked since the read above is gone; one added since then
        # names an unlocked role and is left out of the ceiling.
        held = set(await uow.memberships.lock_role_ids(membership.id))
        for role in roles:
            if role.id in held:
                ceiling |= role.permissions

    if resource_type is not None and resource_id is not None:
        actor_grant = await uow.resource_grants.lock_for_user_resource(
            actor_id, community_id, resource_type, resource_id
        )
        if actor_grant is not None:
            ceiling |= actor_grant.permissions
    return ceiling


def check_permission_ceiling(
    ceiling: set[Permission], conferred: set[Permission]
) -> None:
    """Raise :class:`PermissionCeilingExceededError` listing ``conferred - ceiling``."""
    exceeded = conferred - ceiling
    if exceeded:
        raise PermissionCeilingExceededError(sorted(p.value for p in exceeded))


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

    Locks the ceiling's sources as :func:`lock_actor_ceiling` does, so the write
    that follows in the same transaction commits only on authority the actor
    still holds. Raises :class:`PermissionCeilingExceededError` listing the
    exceeded codes.
    """
    if not conferred:
        return
    ceiling = await lock_actor_ceiling(
        uow,
        actor_id=actor_id,
        community_id=community_id,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    check_permission_ceiling(ceiling, conferred)
