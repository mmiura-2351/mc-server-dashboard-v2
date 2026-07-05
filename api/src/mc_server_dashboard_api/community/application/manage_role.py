"""Role use cases: list / read / create / update / delete custom roles (6.4).

These run *after* the route's two-layer authorization dependency has admitted the
caller (non-member -> 404, member-without-permission -> 403; Section 6.4), so they
assume an authorized member and only do the data work.

- :class:`ListRoles` / :class:`ReadRole` return the community's roles (role:read).
- :class:`CreateRole` creates a custom role; every permission is validated against
  the community-scoped catalog (platform-admin codes rejected — FR-AUTHZ-3/5), and
  a duplicate name surfaces as :class:`RoleAlreadyExistsError` (the unique
  constraint, translated by the UnitOfWork).
- :class:`UpdateRole` renames and/or replaces the permission set with the same
  validation. The preset Owner role is immutable: its set must remain the full
  catalog, so any edit is rejected (the simplest honest guard — issue #71).
- :class:`DeleteRole` deletes a custom role; the preset Owner role is undeletable
  (the same invariant the membership context enforces — a community must keep its
  Owner role). Deleting a non-preset role cascades its ``membership_role`` rows via
  the DB FK (``ondelete=CASCADE``, DATABASE.md Section 10).

Cross-community safety: read/update/delete validate the role belongs to *this*
community (``role.community_id == community_id``), reporting a mismatch as
not-found so no signal about another community's roles leaks (FR-AUTHZ-4).
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.community.application.permission_ceiling import (
    enforce_permission_ceiling,
)
from mc_server_dashboard_api.community.domain.clock import Clock
from mc_server_dashboard_api.community.domain.entities import Role
from mc_server_dashboard_api.community.domain.errors import (
    PresetRoleNotEditableError,
    RoleNotFoundError,
)
from mc_server_dashboard_api.community.domain.permissions import (
    require_community_permission,
)
from mc_server_dashboard_api.community.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    Permission,
    RoleId,
    RoleName,
    UserId,
)


def _validate_permissions(permissions: set[Permission]) -> set[Permission]:
    """Return ``permissions`` if all are community-scoped, else raise.

    Each must be a community-scoped catalog code; a platform-admin or unknown code
    raises ``UnknownPermissionError`` (FR-AUTHZ-3/5).
    """

    return {require_community_permission(perm) for perm in permissions}


@dataclass(frozen=True)
class ListRoles:
    """List the community's roles (role:read)."""

    uow: UnitOfWork

    async def __call__(self, *, community_id: CommunityId) -> list[Role]:
        async with self.uow:
            return await self.uow.roles.list_for_community(community_id)


@dataclass(frozen=True)
class ReadRole:
    """Return a single role by id, scoped to this community (role:read)."""

    uow: UnitOfWork

    async def __call__(self, *, community_id: CommunityId, role_id: RoleId) -> Role:
        async with self.uow:
            role = await self.uow.roles.get_by_id(role_id)
        if role is None or role.community_id != community_id:
            raise RoleNotFoundError(str(role_id.value))
        return role


@dataclass(frozen=True)
class CreateRole:
    """Create a custom role with a validated permission set (role:manage)."""

    uow: UnitOfWork
    clock: Clock

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        actor_id: UserId,
        name: str,
        permissions: set[Permission],
    ) -> Role:
        role_name = RoleName(name)
        validated = _validate_permissions(permissions)
        now = self.clock.now()
        role = Role(
            id=RoleId.new(),
            community_id=community_id,
            name=role_name,
            permissions=validated,
            created_at=now,
            updated_at=now,
            is_preset=False,
        )
        async with self.uow:
            await enforce_permission_ceiling(
                self.uow,
                actor_id=actor_id,
                community_id=community_id,
                conferred=validated,
            )
            await self.uow.roles.add(role)
            await self.uow.commit()
        return role


@dataclass(frozen=True)
class UpdateRole:
    """Rename and/or replace a role's permission set (role:manage).

    The preset Owner role is immutable (issue #71): its permission set must stay the
    full community-scoped catalog, so any edit raises
    :class:`PresetRoleNotEditableError`. A role outside this community is reported
    as not-found (cross-community safety, FR-AUTHZ-4).
    """

    uow: UnitOfWork
    clock: Clock

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        role_id: RoleId,
        actor_id: UserId,
        name: str | None = None,
        permissions: set[Permission] | None = None,
    ) -> Role:
        async with self.uow:
            role = await self.uow.roles.get_by_id(role_id)
            if role is None or role.community_id != community_id:
                raise RoleNotFoundError(str(role_id.value))
            if role.is_preset:
                raise PresetRoleNotEditableError(str(role_id.value))

            if name is not None:
                role.name = RoleName(name)
            if permissions is not None:
                validated = _validate_permissions(permissions)
                conferred = validated - role.permissions
                await enforce_permission_ceiling(
                    self.uow,
                    actor_id=actor_id,
                    community_id=community_id,
                    conferred=conferred,
                )
                role.permissions = validated
            role.updated_at = self.clock.now()
            await self.uow.roles.update(role)
            await self.uow.commit()
        return role


@dataclass(frozen=True)
class DeleteRole:
    """Delete a custom role, cascading its assignments (role:manage).

    The preset Owner role is undeletable: deleting it would strip the community of
    the role that keeps it administrable (the same invariant the membership context
    guards — issue #71). A role outside this community is reported as not-found. A
    non-preset role's ``membership_role`` rows cascade via the DB FK (Section 10).
    """

    uow: UnitOfWork

    async def __call__(self, *, community_id: CommunityId, role_id: RoleId) -> None:
        async with self.uow:
            role = await self.uow.roles.get_by_id(role_id)
            if role is None or role.community_id != community_id:
                raise RoleNotFoundError(str(role_id.value))
            if role.is_preset:
                raise PresetRoleNotEditableError(str(role_id.value))
            await self.uow.roles.delete(role_id)
            await self.uow.commit()
