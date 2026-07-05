"""Membership use cases: add / remove / list members and role assignment (6.3).

These run *after* the route's two-layer authorization dependency has admitted the
caller (non-member -> 404, member-without-permission -> 403; Section 6.4), so they
assume an authorized member and only do the data work.

- :class:`AddMember` manually adds an *existing* user to the community (FR-MEM-1).
  The user is validated against the :class:`UserDirectory` Port first (so an
  unknown user fails cleanly rather than tripping the FK); a duplicate membership
  surfaces as :class:`MembershipAlreadyExistsError`.
- :class:`RemoveMember` deletes the membership and, atomically in one
  ``UnitOfWork`` (FR-MEM-3 / DATABASE.md Section 10), sweeps the member's resource
  grants in *this* community (they FK ``user_id``, not ``membership_id``, so no
  cascade) while ``membership_role`` rows go via DB cascade. It refuses to remove
  the only holder of the preset Owner role, which would orphan the community.
- :class:`ListMembers` returns the community's memberships with their role names.
- :class:`AssignRole` / :class:`UnassignRole` attach/detach a community role to a
  member. They validate the role belongs to *this* community (the
  ``membership_role`` FK accepts any role id, so cross-community assignment must be
  rejected in the use case — FR-AUTHZ-4, security-critical).
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.community.application.permission_ceiling import (
    enforce_permission_ceiling,
)
from mc_server_dashboard_api.community.domain.clock import Clock
from mc_server_dashboard_api.community.domain.entities import Membership, Role
from mc_server_dashboard_api.community.domain.errors import (
    CommunityNotFoundError,
    LastOwnerRemovalError,
    MembershipNotFoundError,
    MemberUserNotFoundError,
    RoleNotFoundError,
)
from mc_server_dashboard_api.community.domain.permissions import OWNER_ROLE_NAME
from mc_server_dashboard_api.community.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.community.domain.user_directory import UserDirectory
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    MembershipId,
    RoleId,
    RoleName,
    UserId,
)


async def _guard_last_owner(
    uow: UnitOfWork, community_id: CommunityId, membership: Membership
) -> None:
    """Enforce the community's last-Owner invariant for ``membership``.

    A community must always retain at least one member holding the preset Owner
    role. This is enforced on member removal and on Owner-role unassignment;
    role deletion will need the same guard (issue #71).

    If ``membership`` holds the preset Owner role and is the only member that
    does, raise :class:`LastOwnerRemovalError`; otherwise return. When the
    community has no preset Owner role the guard is a no-op.
    """
    owner_role = next(
        (
            role
            for role in await uow.roles.list_for_community(community_id)
            if _is_preset_owner(role)
        ),
        None,
    )
    if owner_role is None:
        return
    held = await uow.memberships.list_role_ids(membership.id)
    if owner_role.id not in held:
        return
    # The member holds Owner; refuse if they are the only such holder.
    for other in await uow.memberships.list_for_community(community_id):
        if other.id == membership.id:
            continue
        if owner_role.id in await uow.memberships.list_role_ids(other.id):
            return
    raise LastOwnerRemovalError(str(membership.user_id.value))


def _is_preset_owner(role: Role) -> bool:
    return role.is_preset and role.name == RoleName(OWNER_ROLE_NAME)


@dataclass(frozen=True)
class MemberView:
    """A member of a community with their username and the roles they hold.

    ``username`` is resolved through the :class:`UserDirectory` seam (issue #78).
    It is ``None`` only when the id does not resolve — unreachable in normal
    operation because ``membership.user_id`` FKs ``user.id`` with
    ``ON DELETE CASCADE`` (DATABASE.md Section 5), so a deleted user takes their
    memberships with them; the field is a defensive fallback, not an expected case.
    """

    user_id: UserId
    membership_id: MembershipId
    role_names: list[str]
    username: str | None


@dataclass(frozen=True)
class AddMember:
    """Manually add an existing user to a community (FR-MEM-1, member:add).

    The target is identified by *either* its user id or its exact username
    (issue #355): a community owner who is not a platform admin cannot list users
    to obtain an id, so username resolution gives them a reachable path. Both
    routes funnel into the same :class:`MemberUserNotFoundError` when the user
    does not exist, so a username that matches nobody is rejected exactly like an
    unknown id — no existence differential to enumerate against.
    """

    uow: UnitOfWork
    users: UserDirectory
    clock: Clock

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        user_id: UserId | None = None,
        username: str | None = None,
    ) -> Membership:
        if username is not None:
            resolved = await self.users.resolve_username(username)
            if resolved is None:
                raise MemberUserNotFoundError(username)
            user_id = resolved
        elif user_id is None or not await self.users.exists(user_id):
            raise MemberUserNotFoundError(str(user_id.value) if user_id else "")

        membership = Membership(
            id=MembershipId.new(),
            user_id=user_id,
            community_id=community_id,
            created_at=self.clock.now(),
        )
        async with self.uow:
            await self.uow.memberships.add(membership)
            await self.uow.commit()
        return membership


@dataclass(frozen=True)
class RemoveMember:
    """Remove a member, sweeping their grants in this community atomically (FR-MEM-3).

    Self-removal is allowed: the route's ``member:remove`` permission is the only
    gate, so a member who holds it may remove themselves. The single guard is the
    last-Owner check, which protects the community from being orphaned regardless
    of who triggers the removal.
    """

    uow: UnitOfWork

    async def __call__(self, *, community_id: CommunityId, user_id: UserId) -> None:
        async with self.uow:
            membership = await self.uow.memberships.get_by_user_and_community(
                user_id, community_id
            )
            if membership is None:
                raise MembershipNotFoundError(str(user_id.value))

            await _guard_last_owner(self.uow, community_id, membership)

            # membership_role rows cascade from the membership delete; the grants
            # FK user_id (not membership_id) so they need the explicit sweep — both
            # in this one transaction (DATABASE.md Section 10).
            await self.uow.memberships.delete(membership.id)
            await self.uow.resource_grants.delete_for_user_in_community(
                user_id, community_id
            )
            await self.uow.commit()


@dataclass(frozen=True)
class ListMembers:
    """List the community's members with username and role names (member:read).

    Usernames are resolved through the :class:`UserDirectory` seam in one batch
    lookup for all members (issue #78), never one lookup per member.
    """

    uow: UnitOfWork
    users: UserDirectory

    async def __call__(self, *, community_id: CommunityId) -> list[MemberView]:
        async with self.uow:
            community = await self.uow.communities.get_by_id(community_id)
            if community is None:
                raise CommunityNotFoundError(str(community_id.value))
            roles = {
                role.id: role
                for role in await self.uow.roles.list_for_community(community_id)
            }
            memberships = await self.uow.memberships.list_for_community(community_id)
            role_ids_by_membership = {
                membership.id: await self.uow.memberships.list_role_ids(membership.id)
                for membership in memberships
            }
        usernames = await self.users.usernames_for(
            [membership.user_id for membership in memberships]
        )
        return [
            MemberView(
                user_id=membership.user_id,
                membership_id=membership.id,
                role_names=[
                    roles[role_id].name.value
                    for role_id in role_ids_by_membership[membership.id]
                    if role_id in roles
                ],
                username=usernames.get(membership.user_id),
            )
            for membership in memberships
        ]


@dataclass(frozen=True)
class AssignRole:
    """Assign a community role to a member (role:manage).

    Validates the role belongs to *this* community before staging the assignment:
    the ``membership_role`` FK accepts any role id, so without this check a role
    from another community could be assigned, leaking permissions across the
    isolation boundary (FR-AUTHZ-4). A role absent from this community is reported
    as not-found, giving no signal about other communities' roles.
    """

    uow: UnitOfWork

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        user_id: UserId,
        role_id: RoleId,
        actor_id: UserId,
    ) -> None:
        async with self.uow:
            membership = await self.uow.memberships.get_by_user_and_community(
                user_id, community_id
            )
            if membership is None:
                raise MembershipNotFoundError(str(user_id.value))

            role = await self.uow.roles.get_by_id(role_id)
            if role is None or role.community_id != community_id:
                raise RoleNotFoundError(str(role_id.value))

            await enforce_permission_ceiling(
                self.uow,
                actor_id=actor_id,
                community_id=community_id,
                conferred=role.permissions,
            )

            if role_id in await self.uow.memberships.list_role_ids(membership.id):
                return  # idempotent: already assigned, nothing to commit
            await self.uow.memberships.assign_role(membership.id, role_id)
            await self.uow.commit()


@dataclass(frozen=True)
class UnassignRole:
    """Unassign a community role from a member (role:manage).

    Like :class:`AssignRole`, validates the role belongs to this community so a
    caller cannot probe another community's role ids. When the role is the preset
    Owner role, the last-Owner invariant is enforced so a caller cannot orphan
    the community by stripping Owner from its sole holder.
    """

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, user_id: UserId, role_id: RoleId
    ) -> None:
        async with self.uow:
            membership = await self.uow.memberships.get_by_user_and_community(
                user_id, community_id
            )
            if membership is None:
                raise MembershipNotFoundError(str(user_id.value))

            role = await self.uow.roles.get_by_id(role_id)
            if role is None or role.community_id != community_id:
                raise RoleNotFoundError(str(role_id.value))

            if _is_preset_owner(role):
                await _guard_last_owner(self.uow, community_id, membership)

            await self.uow.memberships.unassign_role(membership.id, role_id)
            await self.uow.commit()
