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

0. the recipient of a role assignment or a grant creation, held against account
   deletion by its account write lock (``UnitOfWork.users``, issues
   #3217, #3856), before any lock of this context. A grant creation that held
   the recipient's membership first would deadlock with the deletion: the
   deletion's cascade waits for that membership, and the grant's foreign-key
   check waits for the user row the deletion is deleting;
1. the community row (``FOR KEY SHARE``, :func:`hold_community`; issue #4021),
   held by every write that locks a row a community deletion cascades to and
   can then wait for another: a role creation or update, a role assignment, a
   grant creation, a member removal and a role unassignment take it before
   their first other lock of this context, and :func:`lock_actor_ceiling`
   expects it held. A rename is one: its UPDATE locks the role row, and its new
   name's unique entry can then wait for a deletion that deleted a role of that
   name;
2. memberships: the actor's, and the recipient's a role assignment or grant
   creation inserts its row under (``FOR KEY SHARE``, one statement in
   ascending id order, taken by the use case before anything else but steps 0
   and 1);
3. the grant's resource (``FOR KEY SHARE``, grant creation's existence check);
4. role rows, one at a time in ascending id order (``FOR SHARE``; the role whose
   permissions an update replaces ``FOR UPDATE``, in its place in the order);
5. the actor's ``membership_role`` rows of the roles locked in step 4 only
   (``FOR SHARE``; one added since names a role never locked, and is skipped);
6. the actor's grant on the resource (``FOR SHARE``).

Each row is taken at its final strength in one pass, so two actors editing each
other's roles cannot deadlock, and the inserting write's foreign-key checks find
their parents (the memberships, the server, the assigned role) already held.
Against the deleters:

- a member removal holds the community row, then locks the membership
  (``FOR UPDATE``), then its last-owner guard locks the Owner assignments, then
  its cascade the membership's assignments and grants -- the order above;
- a role deletion locks the role, then its cascade the assignments, in one
  statement;
- a server deletion locks the server, then its cascade the grants;
- a role unassignment holds the community row, then locks only assignments (the
  Owner ones first, under the last-owner guard);
- an account deletion takes the account write lock (exclusive) right after its
  ``user`` row locks and before any other, and its cascade then locks the
  account's memberships, their assignments and its grants -- the order above.
  A deletion waiting at step 0 holds only ``user`` rows
  (``FOR NO KEY UPDATE``), which no transaction of this context waits for;
- a community deletion's DELETE locks the community row, then the rows it
  cascades to (issue #4021): every table here, and it is refused while the
  community holds a server (issue #3218) only once it has cascaded to the
  rest. The schema does not fix the order of those cascades -- a row's run in
  the order of their trigger names, which embed OIDs -- so a write that locks
  one of those rows and can then wait for another holds the community row first
  (step 1). Whichever of the two locks it first, the other waits there holding
  nothing the first needs. A role deletion takes no such hold: a deletion
  deletes every role before any assignment, so it never holds an assignment
  the role deletion waits for while it waits for the role.

A write that waited for the deletion, or began after it committed, finds the
community gone at step 1 and raises :class:`CommunityNotFoundError` (issue
#2967). So does a member addition, whose INSERT is its only lock of this
context: it meets the deletion at its foreign key onto ``community``
(``adapters/integrity.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from mc_server_dashboard_api.community.domain.errors import (
    CommunityNotFoundError,
    PermissionCeilingExceededError,
)
from mc_server_dashboard_api.community.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    Permission,
    RoleId,
    UserId,
)


async def hold_community(uow: UnitOfWork, community_id: CommunityId) -> None:
    """Hold the community against its deletion until the transaction ends.

    Step 1 of the lock order. Raises :class:`CommunityNotFoundError` when the
    community is gone: a deletion committed first, or committed while this
    waited for it.
    """
    if not await uow.communities.hold(community_id):
        raise CommunityNotFoundError(str(community_id.value))


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
    if the actor holds them.

    The caller holds the community first (lock order step 1), so an actor
    without a membership is not a member of a community that exists, and gets
    the empty ceiling. A caller that also inserts under a recipient's membership
    holds both memberships first too (step 2); the actor's is then already held
    here.
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
        # Locked after the roles, as a role deletion's cascade locks them, and
        # only those of the roles just locked: an assignment added since the
        # read above names a role this pass never locked, so it is neither
        # locked (that would be out of order) nor counted. An assignment revoked
        # since then is gone.
        held = set(
            await uow.memberships.lock_role_ids(
                membership.id, [role.id for role in roles]
            )
        )
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

    Locks the ceiling's sources as :func:`lock_actor_ceiling` does, the caller
    holding the community first, so the write that follows in the same
    transaction commits only on authority the actor still holds. Raises
    :class:`PermissionCeilingExceededError` listing the exceeded codes.
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
