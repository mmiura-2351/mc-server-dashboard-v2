"""Async-SQLAlchemy implementations of the community repository Ports.

Each repository works on an ``AsyncSession`` owned by the enclosing
``UnitOfWork``; it stages rows and runs reads but never commits — commit is the
unit of work's job (DATABASE.md Section 1). Rows are translated to/from the
framework-free domain entities here.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mc_server_dashboard_api.community.adapters.models import (
    CommunityModel,
    MembershipModel,
    MembershipRoleModel,
    ResourceGrantModel,
    RoleModel,
)
from mc_server_dashboard_api.community.domain.entities import (
    Community,
    Membership,
    ResourceGrant,
    Role,
)
from mc_server_dashboard_api.community.domain.repositories import (
    CommunityRepository,
    MembershipRepository,
    ResourceGrantRepository,
    RoleRepository,
)
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


def _to_community(row: CommunityModel) -> Community:
    return Community(
        id=CommunityId(row.id),
        name=CommunityName(row.name),
        max_servers=row.max_servers,
        max_members=row.max_members,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_membership(row: MembershipModel) -> Membership:
    return Membership(
        id=MembershipId(row.id),
        user_id=UserId(row.user_id),
        community_id=CommunityId(row.community_id),
        created_at=row.created_at,
    )


def _to_role(row: RoleModel) -> Role:
    return Role(
        id=RoleId(row.id),
        community_id=CommunityId(row.community_id),
        name=RoleName(row.name),
        permissions={Permission(code) for code in row.permissions},
        is_preset=row.is_preset,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_resource_grant(row: ResourceGrantModel) -> ResourceGrant:
    return ResourceGrant(
        id=ResourceGrantId(row.id),
        user_id=UserId(row.user_id),
        community_id=CommunityId(row.community_id),
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        permissions={Permission(code) for code in row.permissions},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyCommunityRepository(CommunityRepository):
    """:class:`CommunityRepository` adapter over an ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, community: Community) -> None:
        self._session.add(
            CommunityModel(
                id=community.id.value,
                name=community.name.value,
                max_servers=community.max_servers,
                max_members=community.max_members,
                created_at=community.created_at,
                updated_at=community.updated_at,
            )
        )

    async def get_by_id(self, community_id: CommunityId) -> Community | None:
        row = await self._session.get(CommunityModel, community_id.value)
        return _to_community(row) if row is not None else None

    async def get_by_name(self, name: CommunityName) -> Community | None:
        stmt = select(CommunityModel).where(CommunityModel.name == name.value)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_community(row) if row is not None else None

    async def update(self, community: Community) -> None:
        stmt = (
            update(CommunityModel)
            .where(CommunityModel.id == community.id.value)
            .values(name=community.name.value, updated_at=community.updated_at)
        )
        await self._session.execute(stmt)

    async def delete(self, community_id: CommunityId) -> None:
        stmt = delete(CommunityModel).where(CommunityModel.id == community_id.value)
        await self._session.execute(stmt)


class SqlAlchemyMembershipRepository(MembershipRepository):
    """:class:`MembershipRepository` adapter over an ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, membership: Membership) -> None:
        self._session.add(
            MembershipModel(
                id=membership.id.value,
                user_id=membership.user_id.value,
                community_id=membership.community_id.value,
                created_at=membership.created_at,
            )
        )

    async def get_by_id(self, membership_id: MembershipId) -> Membership | None:
        row = await self._session.get(MembershipModel, membership_id.value)
        return _to_membership(row) if row is not None else None

    async def get_by_user_and_community(
        self, user_id: UserId, community_id: CommunityId
    ) -> Membership | None:
        stmt = select(MembershipModel).where(
            MembershipModel.user_id == user_id.value,
            MembershipModel.community_id == community_id.value,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_membership(row) if row is not None else None

    async def list_for_user(self, user_id: UserId) -> list[Membership]:
        stmt = select(MembershipModel).where(MembershipModel.user_id == user_id.value)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_membership(row) for row in rows]

    async def list_for_community(self, community_id: CommunityId) -> list[Membership]:
        stmt = select(MembershipModel).where(
            MembershipModel.community_id == community_id.value
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_membership(row) for row in rows]

    async def delete(self, membership_id: MembershipId) -> None:
        stmt = delete(MembershipModel).where(MembershipModel.id == membership_id.value)
        await self._session.execute(stmt)

    async def assign_role(self, membership_id: MembershipId, role_id: RoleId) -> None:
        self._session.add(
            MembershipRoleModel(
                membership_id=membership_id.value, role_id=role_id.value
            )
        )

    async def unassign_role(self, membership_id: MembershipId, role_id: RoleId) -> None:
        stmt = delete(MembershipRoleModel).where(
            MembershipRoleModel.membership_id == membership_id.value,
            MembershipRoleModel.role_id == role_id.value,
        )
        await self._session.execute(stmt)

    async def list_role_ids(self, membership_id: MembershipId) -> list[RoleId]:
        stmt = select(MembershipRoleModel.role_id).where(
            MembershipRoleModel.membership_id == membership_id.value
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [RoleId(row) for row in rows]


class SqlAlchemyRoleRepository(RoleRepository):
    """:class:`RoleRepository` adapter over an ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, role: Role) -> None:
        self._session.add(
            RoleModel(
                id=role.id.value,
                community_id=role.community_id.value,
                name=role.name.value,
                permissions=sorted(perm.value for perm in role.permissions),
                is_preset=role.is_preset,
                created_at=role.created_at,
                updated_at=role.updated_at,
            )
        )

    async def get_by_id(self, role_id: RoleId) -> Role | None:
        row = await self._session.get(RoleModel, role_id.value)
        return _to_role(row) if row is not None else None

    async def list_for_community(self, community_id: CommunityId) -> list[Role]:
        stmt = select(RoleModel).where(RoleModel.community_id == community_id.value)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_role(row) for row in rows]

    async def update(self, role: Role) -> None:
        stmt = (
            update(RoleModel)
            .where(RoleModel.id == role.id.value)
            .values(
                name=role.name.value,
                permissions=sorted(perm.value for perm in role.permissions),
                updated_at=role.updated_at,
            )
        )
        await self._session.execute(stmt)

    async def delete(self, role_id: RoleId) -> None:
        stmt = delete(RoleModel).where(RoleModel.id == role_id.value)
        await self._session.execute(stmt)


class SqlAlchemyResourceGrantRepository(ResourceGrantRepository):
    """:class:`ResourceGrantRepository` adapter over an ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, grant: ResourceGrant) -> None:
        self._session.add(
            ResourceGrantModel(
                id=grant.id.value,
                user_id=grant.user_id.value,
                community_id=grant.community_id.value,
                resource_type=grant.resource_type,
                resource_id=grant.resource_id,
                permissions=sorted(perm.value for perm in grant.permissions),
                created_at=grant.created_at,
                updated_at=grant.updated_at,
            )
        )

    async def get_by_id(self, grant_id: ResourceGrantId) -> ResourceGrant | None:
        row = await self._session.get(ResourceGrantModel, grant_id.value)
        return _to_resource_grant(row) if row is not None else None

    async def get_for_user_resource(
        self,
        user_id: UserId,
        community_id: CommunityId,
        resource_type: str,
        resource_id: uuid.UUID,
    ) -> ResourceGrant | None:
        stmt = select(ResourceGrantModel).where(
            ResourceGrantModel.user_id == user_id.value,
            ResourceGrantModel.community_id == community_id.value,
            ResourceGrantModel.resource_type == resource_type,
            ResourceGrantModel.resource_id == resource_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_resource_grant(row) if row is not None else None

    async def list_for_community(
        self, community_id: CommunityId, user_id: UserId | None = None
    ) -> list[ResourceGrant]:
        stmt = select(ResourceGrantModel).where(
            ResourceGrantModel.community_id == community_id.value
        )
        if user_id is not None:
            stmt = stmt.where(ResourceGrantModel.user_id == user_id.value)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_resource_grant(row) for row in rows]

    async def delete(self, grant_id: ResourceGrantId) -> None:
        stmt = delete(ResourceGrantModel).where(ResourceGrantModel.id == grant_id.value)
        await self._session.execute(stmt)

    async def delete_for_user_in_community(
        self, user_id: UserId, community_id: CommunityId
    ) -> None:
        stmt = delete(ResourceGrantModel).where(
            ResourceGrantModel.user_id == user_id.value,
            ResourceGrantModel.community_id == community_id.value,
        )
        await self._session.execute(stmt)

    async def delete_for_resource(
        self, resource_type: str, resource_id: uuid.UUID
    ) -> None:
        stmt = delete(ResourceGrantModel).where(
            ResourceGrantModel.resource_type == resource_type,
            ResourceGrantModel.resource_id == resource_id,
        )
        await self._session.execute(stmt)
