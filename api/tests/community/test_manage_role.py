"""Unit tests for the role use cases (FR-AUTHZ-3/4, issue #71).

Against the in-memory fakes (TESTING.md Section 4). The authorization gate lives
in the route dependency, so these verify only the data behaviour: create/update
validate the permission set against the community-scoped catalog (platform-admin
and unknown codes rejected); the preset Owner role is immutable and undeletable;
read/update/delete reject a role from another community as not-found
(cross-community safety, FR-AUTHZ-4).
"""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.community.application.manage_role import (
    CreateRole,
    DeleteRole,
    ListRoles,
    ReadRole,
    UpdateRole,
)
from mc_server_dashboard_api.community.domain.clock import Clock
from mc_server_dashboard_api.community.domain.entities import Role
from mc_server_dashboard_api.community.domain.errors import (
    PresetRoleNotEditableError,
    RoleNotFoundError,
    UnknownPermissionError,
)
from mc_server_dashboard_api.community.domain.permissions import (
    COMMUNITY_PERMISSIONS,
    OWNER_ROLE_NAME,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    Permission,
    RoleId,
    RoleName,
)
from tests.community.fakes import FakeAuthzUnitOfWork

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


class _FakeClock(Clock):
    def now(self) -> dt.datetime:
        return _NOW


def _seed_owner_role(uow: FakeAuthzUnitOfWork, community_id: CommunityId) -> RoleId:
    role = Role(
        id=RoleId.new(),
        community_id=community_id,
        name=RoleName(OWNER_ROLE_NAME),
        permissions=set(COMMUNITY_PERMISSIONS),
        created_at=_NOW,
        updated_at=_NOW,
        is_preset=True,
    )
    uow.roles.by_id[role.id] = role
    return role.id


def _seed_custom_role(
    uow: FakeAuthzUnitOfWork,
    community_id: CommunityId,
    *,
    name: str = "Editor",
    permissions: set[Permission] | None = None,
) -> RoleId:
    role = Role(
        id=RoleId.new(),
        community_id=community_id,
        name=RoleName(name),
        permissions=permissions or {Permission("server:read")},
        created_at=_NOW,
        updated_at=_NOW,
        is_preset=False,
    )
    uow.roles.by_id[role.id] = role
    return role.id


async def test_create_role_persists_a_validated_role() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    role = await CreateRole(uow=uow, clock=_FakeClock())(
        community_id=community,
        name="Editor",
        permissions={Permission("server:read"), Permission("server:start")},
    )
    assert role.community_id == community
    assert role.is_preset is False
    assert uow.roles.by_id[role.id].name == RoleName("Editor")
    assert uow.commits == 1


async def test_create_role_rejects_unknown_permission() -> None:
    uow = FakeAuthzUnitOfWork()
    with pytest.raises(UnknownPermissionError):
        await CreateRole(uow=uow, clock=_FakeClock())(
            community_id=CommunityId.new(),
            name="Bad",
            permissions={Permission("server:teleport")},
        )
    assert uow.commits == 0


async def test_create_role_rejects_platform_admin_permission() -> None:
    uow = FakeAuthzUnitOfWork()
    with pytest.raises(UnknownPermissionError):
        await CreateRole(uow=uow, clock=_FakeClock())(
            community_id=CommunityId.new(),
            name="Bad",
            permissions={Permission("worker:manage")},
        )
    assert uow.commits == 0


async def test_update_role_replaces_name_and_permissions() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    role_id = _seed_custom_role(uow, community)
    role = await UpdateRole(uow=uow, clock=_FakeClock())(
        community_id=community,
        role_id=role_id,
        name="Operator",
        permissions={Permission("server:stop")},
    )
    assert role.name == RoleName("Operator")
    assert role.permissions == {Permission("server:stop")}


async def test_update_role_rejects_editing_the_preset_owner_role() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    owner_id = _seed_owner_role(uow, community)
    with pytest.raises(PresetRoleNotEditableError):
        await UpdateRole(uow=uow, clock=_FakeClock())(
            community_id=community,
            role_id=owner_id,
            permissions={Permission("server:read")},
        )


async def test_update_role_rejects_unknown_permission() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    role_id = _seed_custom_role(uow, community)
    with pytest.raises(UnknownPermissionError):
        await UpdateRole(uow=uow, clock=_FakeClock())(
            community_id=community,
            role_id=role_id,
            permissions={Permission("server:teleport")},
        )


async def test_update_role_in_other_community_is_not_found() -> None:
    uow = FakeAuthzUnitOfWork()
    role_id = _seed_custom_role(uow, CommunityId.new())
    with pytest.raises(RoleNotFoundError):
        await UpdateRole(uow=uow, clock=_FakeClock())(
            community_id=CommunityId.new(),
            role_id=role_id,
            name="X",
        )


async def test_delete_role_removes_a_custom_role() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    role_id = _seed_custom_role(uow, community)
    await DeleteRole(uow=uow)(community_id=community, role_id=role_id)
    assert role_id not in uow.roles.by_id


async def test_delete_role_rejects_the_preset_owner_role() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    owner_id = _seed_owner_role(uow, community)
    with pytest.raises(PresetRoleNotEditableError):
        await DeleteRole(uow=uow)(community_id=community, role_id=owner_id)
    assert owner_id in uow.roles.by_id


async def test_delete_role_in_other_community_is_not_found() -> None:
    uow = FakeAuthzUnitOfWork()
    role_id = _seed_custom_role(uow, CommunityId.new())
    with pytest.raises(RoleNotFoundError):
        await DeleteRole(uow=uow)(community_id=CommunityId.new(), role_id=role_id)


async def test_read_role_returns_the_role() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    role_id = _seed_custom_role(uow, community)
    role = await ReadRole(uow=uow)(community_id=community, role_id=role_id)
    assert role.id == role_id


async def test_read_role_in_other_community_is_not_found() -> None:
    uow = FakeAuthzUnitOfWork()
    role_id = _seed_custom_role(uow, CommunityId.new())
    with pytest.raises(RoleNotFoundError):
        await ReadRole(uow=uow)(community_id=CommunityId.new(), role_id=role_id)


async def test_list_roles_scopes_to_the_community() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    _seed_custom_role(uow, community, name="A")
    _seed_custom_role(uow, CommunityId.new(), name="B")
    roles = await ListRoles(uow=uow)(community_id=community)
    assert {r.name.value for r in roles} == {"A"}
