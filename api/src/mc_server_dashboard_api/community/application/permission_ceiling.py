"""Grant-only-what-you-hold ceiling enforcement (issue #1595).

When a role or grant is created/updated, the permissions being conferred must
be a subset of the actor's own effective permissions in the community.  This
prevents a ``role:manage`` or ``grant:manage`` holder from escalating to
owner-equivalent control by conferring permissions they do not possess.

The ceiling is read under lock (#3241): the rows it is computed from -- the
actor's membership, their roles, their assignments of those roles, and their
grant on the resource -- stay locked until the conferring write commits. A
revocation of any of them (a permission removed from the role, the role
unassigned or deleted, the member removed, the grant revoked) therefore either
commits first, and the ceiling sees it, or waits until the conferral has
committed; a conferral never commits after the authority it relied on was
revoked. A role assignment also share-locks the role it assigns, so the set it
confers is the one it commits.

Lock order, the same for every transaction that locks through here:

1. memberships: the actor's, and the recipient's a role assignment or grant
   creation inserts its row under (``FOR KEY SHARE``, one statement in
   ascending id order, taken by the use case before anything else);
2. the grant's resource (``FOR KEY SHARE``, grant creation's existence check);
3. role rows, one at a time in ascending id order (``FOR SHARE``; the role whose
   permissions an update replaces ``FOR UPDATE``, in its place in the order);
4. the actor's ``membership_role`` rows (``FOR SHARE``);
5. the actor's grant on the resource (``FOR SHARE``).

Each row is taken at its final strength in one pass, so two actors editing each
other's roles cannot deadlock, and the inserting write's foreign-key checks find
their parents (the memberships, the server, the assigned role) already held.
Against the deleters:

- a member removal locks the membership first (``FOR UPDATE``), then its
  last-owner guard locks the Owner assignments, then its cascade the
  membership's assignments and grants -- the order above;
- a role deletion locks the role, then its cascade the assignments;
- a server deletion locks the server, then its cascade the grants;
- a role unassignment locks only assignments (the Owner ones first, under the
  last-owner guard).

A community deletion cascades through every table and is not ordered with
these; a deadlock there is detected by PostgreSQL and aborts one side.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

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
    roles_to_share: Sequence[RoleId] = (),
) -> set[Permission]:
    """Return the actor's ceiling, its sources locked until the transaction ends.

    The ceiling is the union of the permissions of the actor's roles in the
    community. When ``resource_type`` and ``resource_id`` are given (grant
    operations), the actor's own grant on that exact resource also counts.

    ``role_to_update`` is locked ``FOR UPDATE``, and ``roles_to_share`` share-
    locked, in the same ordered pass as the actor's roles, for a write that
    depends on those roles' current state; they count toward the ceiling only
    if the actor holds them. A caller that also inserts under a recipient's
    membership holds both memberships first (lock order step 1); the actor's
    is then already held here.
    """
    memberships = await uow.memberships.hold_for_users(community_id, [actor_id])
    membership = memberships[0] if memberships else None
    role_ids = (
        [] if membership is None else await uow.memberships.list_role_ids(membership.id)
    )
    roles = await uow.roles.lock_by_ids(
        [*role_ids, *roles_to_share], for_update=role_to_update
    )
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
