"""In-memory fakes for the community Ports used by the authorization tests.

Keep the evaluator under test against fakes (no database), per TESTING.md
Section 4. The fakes implement only what the PermissionChecker / membership
visibility evaluator reaches through the UnitOfWork; the helpers below seed
members, roles, and grants concisely.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from mc_server_dashboard_api.community.domain.entities import (
    Community,
    Membership,
    ResourceGrant,
    Role,
)
from mc_server_dashboard_api.community.domain.repositories import (
    CommunityRepository,
    MembershipRepository,
    ResourceExistenceChecker,
    ResourceGrantRepository,
    RoleRepository,
)
from mc_server_dashboard_api.community.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    CommunityName,
    MembershipId,
    Permission,
    ResourceGrantId,
    RoleId,
    RoleName,
    UserId,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


class FakeCommunityRepository(CommunityRepository):
    def __init__(self) -> None:
        self.by_id: dict[CommunityId, Community] = {}

    async def add(self, community: Community) -> None:
        self.by_id[community.id] = community

    async def get_by_id(self, community_id: CommunityId) -> Community | None:
        return self.by_id.get(community_id)

    async def get_by_name(self, name: CommunityName) -> Community | None:
        for community in self.by_id.values():
            if community.name == name:
                return community
        return None

    async def update(self, community: Community) -> None:
        self.by_id[community.id] = community

    async def delete(self, community_id: CommunityId) -> None:
        self.by_id.pop(community_id, None)


class FakeMembershipRepository(MembershipRepository):
    def __init__(self) -> None:
        self.by_id: dict[MembershipId, Membership] = {}
        self.role_ids: dict[MembershipId, list[RoleId]] = {}

    async def add(self, membership: Membership) -> None:
        self.by_id[membership.id] = membership

    async def get_by_id(self, membership_id: MembershipId) -> Membership | None:
        return self.by_id.get(membership_id)

    async def get_by_user_and_community(
        self, user_id: UserId, community_id: CommunityId
    ) -> Membership | None:
        for membership in self.by_id.values():
            if (
                membership.user_id == user_id
                and membership.community_id == community_id
            ):
                return membership
        return None

    async def list_for_user(self, user_id: UserId) -> list[Membership]:
        return [m for m in self.by_id.values() if m.user_id == user_id]

    async def list_for_community(self, community_id: CommunityId) -> list[Membership]:
        return [m for m in self.by_id.values() if m.community_id == community_id]

    async def delete(self, membership_id: MembershipId) -> None:
        self.by_id.pop(membership_id, None)
        self.role_ids.pop(membership_id, None)

    async def assign_role(self, membership_id: MembershipId, role_id: RoleId) -> None:
        self.role_ids.setdefault(membership_id, []).append(role_id)

    async def unassign_role(self, membership_id: MembershipId, role_id: RoleId) -> None:
        ids = self.role_ids.get(membership_id)
        if ids is not None and role_id in ids:
            ids.remove(role_id)

    async def list_role_ids(self, membership_id: MembershipId) -> list[RoleId]:
        return list(self.role_ids.get(membership_id, []))


class FakeRoleRepository(RoleRepository):
    def __init__(self) -> None:
        self.by_id: dict[RoleId, Role] = {}
        # Spies for the N+1 query-count assertion (issue #321).
        self.get_by_id_calls = 0
        self.get_by_ids_calls = 0

    async def add(self, role: Role) -> None:
        self.by_id[role.id] = role

    async def get_by_id(self, role_id: RoleId) -> Role | None:
        self.get_by_id_calls += 1
        return self.by_id.get(role_id)

    async def get_by_ids(self, role_ids: Sequence[RoleId]) -> list[Role]:
        self.get_by_ids_calls += 1
        if not role_ids:
            return []
        return [self.by_id[rid] for rid in role_ids if rid in self.by_id]

    async def list_for_community(self, community_id: CommunityId) -> list[Role]:
        return [r for r in self.by_id.values() if r.community_id == community_id]

    async def update(self, role: Role) -> None:
        self.by_id[role.id] = role

    async def delete(self, role_id: RoleId) -> None:
        self.by_id.pop(role_id, None)


class FakeResourceGrantRepository(ResourceGrantRepository):
    def __init__(self) -> None:
        self.by_id: dict[ResourceGrantId, ResourceGrant] = {}

    async def add(self, grant: ResourceGrant) -> None:
        self.by_id[grant.id] = grant

    async def get_by_id(self, grant_id: ResourceGrantId) -> ResourceGrant | None:
        return self.by_id.get(grant_id)

    async def get_for_user_resource(
        self,
        user_id: UserId,
        community_id: CommunityId,
        resource_type: str,
        resource_id: uuid.UUID,
    ) -> ResourceGrant | None:
        for grant in self.by_id.values():
            if (
                grant.user_id == user_id
                and grant.community_id == community_id
                and grant.resource_type == resource_type
                and grant.resource_id == resource_id
            ):
                return grant
        return None

    async def list_for_community(
        self, community_id: CommunityId, user_id: UserId | None = None
    ) -> list[ResourceGrant]:
        return [
            g
            for g in self.by_id.values()
            if g.community_id == community_id
            and (user_id is None or g.user_id == user_id)
        ]

    async def delete(self, grant_id: ResourceGrantId) -> None:
        self.by_id.pop(grant_id, None)

    async def delete_for_user_in_community(
        self, user_id: UserId, community_id: CommunityId
    ) -> None:
        self.by_id = {
            gid: g
            for gid, g in self.by_id.items()
            if not (g.user_id == user_id and g.community_id == community_id)
        }

    async def delete_for_resource(
        self, resource_type: str, resource_id: uuid.UUID
    ) -> None:
        self.by_id = {
            gid: g
            for gid, g in self.by_id.items()
            if not (g.resource_type == resource_type and g.resource_id == resource_id)
        }


class FakeResourceExistenceChecker(ResourceExistenceChecker):
    """In-memory resource-existence check keyed by ``(community, type, id)``.

    Tests seed the resources a grant may target. The default is empty, so a grant
    on an unseeded resource is rejected (issue #361); seed with
    :meth:`FakeAuthzUnitOfWork.add_resource`.
    """

    def __init__(self) -> None:
        self.existing: set[tuple[CommunityId, str, uuid.UUID]] = set()

    async def exists(
        self, community_id: CommunityId, resource_type: str, resource_id: uuid.UUID
    ) -> bool:
        return (community_id, resource_type, resource_id) in self.existing


class FakeAuthzUnitOfWork(UnitOfWork):
    """In-memory :class:`UnitOfWork` sharing its repositories across blocks."""

    communities: FakeCommunityRepository
    memberships: FakeMembershipRepository
    roles: FakeRoleRepository
    resource_grants: FakeResourceGrantRepository
    resources: FakeResourceExistenceChecker

    def __init__(self) -> None:
        self.communities = FakeCommunityRepository()
        self.memberships = FakeMembershipRepository()
        self.roles = FakeRoleRepository()
        self.resource_grants = FakeResourceGrantRepository()
        self.resources = FakeResourceExistenceChecker()
        self.commits = 0

    async def __aenter__(self) -> "FakeAuthzUnitOfWork":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    # --- seeding helpers ---------------------------------------------------

    def add_role(
        self,
        user_id: UserId,
        community_id: CommunityId,
        permissions: set[Permission],
        *,
        name: str | None = None,
    ) -> RoleId:
        """Create a role with ``permissions`` and assign it to the member."""

        membership = self._membership_for(user_id, community_id)
        role = Role(
            id=RoleId.new(),
            community_id=community_id,
            name=RoleName(name or f"role-{uuid.uuid4()}"),
            permissions=set(permissions),
            created_at=_NOW,
            updated_at=_NOW,
        )
        self.roles.by_id[role.id] = role
        self.memberships.role_ids.setdefault(membership.id, []).append(role.id)
        return role.id

    def add_resource(
        self,
        community_id: CommunityId,
        resource_type: str,
        resource_id: uuid.UUID,
    ) -> None:
        """Mark ``(resource_type, resource_id)`` as existing in ``community_id``."""

        self.resources.existing.add((community_id, resource_type, resource_id))

    def add_grant(
        self,
        user_id: UserId,
        community_id: CommunityId,
        resource_type: str,
        resource_id: uuid.UUID,
        permissions: set[Permission],
    ) -> ResourceGrantId:
        """Create a resource grant on ``(resource_type, resource_id)`` for the user."""

        grant = ResourceGrant(
            id=ResourceGrantId.new(),
            user_id=user_id,
            community_id=community_id,
            resource_type=resource_type,
            resource_id=resource_id,
            permissions=set(permissions),
            created_at=_NOW,
            updated_at=_NOW,
        )
        self.resource_grants.by_id[grant.id] = grant
        return grant.id

    def _membership_for(self, user_id: UserId, community_id: CommunityId) -> Membership:
        existing = next(
            (
                m
                for m in self.memberships.by_id.values()
                if m.user_id == user_id and m.community_id == community_id
            ),
            None,
        )
        if existing is not None:
            return existing
        membership = Membership(
            id=MembershipId.new(),
            user_id=user_id,
            community_id=community_id,
            created_at=_NOW,
        )
        self.memberships.by_id[membership.id] = membership
        return membership


def seed_member_with_role(
    uow: FakeAuthzUnitOfWork,
    user_id: UserId,
    community_id: CommunityId,
    permissions: set[Permission],
) -> None:
    """Make ``user_id`` a member of ``community_id`` holding a role of ``permissions``.

    An empty ``permissions`` set makes them a member with no Layer-2 permissions
    (still a member — visible at Layer-1).
    """

    uow.add_role(user_id, community_id, permissions)
