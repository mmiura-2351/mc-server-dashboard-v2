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
import uuid

import pytest

from mc_server_dashboard_api.community.application.manage_role import (
    CreateRole,
    DeleteRole,
    ListRoles,
    ReadRole,
    UpdateRole,
)
from mc_server_dashboard_api.community.domain.clock import Clock
from mc_server_dashboard_api.community.domain.entities import Community, Role
from mc_server_dashboard_api.community.domain.errors import (
    CommunityNotFoundError,
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
    CommunityName,
    Permission,
    RoleId,
    RoleName,
    UserId,
)
from tests.community.fakes import FakeAuthzUnitOfWork, FakeRoleRepository

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


class _FakeClock(Clock):
    def now(self) -> dt.datetime:
        return _NOW


def _seed_community(uow: FakeAuthzUnitOfWork, community_id: CommunityId) -> None:
    # A role write holds its community first, and reports a missing one gone
    # (issues #2967, #4021).
    uow.communities.seed(
        Community(
            id=community_id,
            name=CommunityName("guild"),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


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
    uow.roles.seed(role)
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
    uow.roles.seed(role)
    return role.id


async def test_create_role_persists_a_validated_role() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    actor = UserId(uuid.uuid4())
    uow.add_role(
        actor,
        community,
        {Permission("server:read"), Permission("server:start")},
    )
    role = await CreateRole(uow=uow, clock=_FakeClock())(
        community_id=community,
        actor_id=actor,
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
            actor_id=UserId(uuid.uuid4()),
            name="Bad",
            permissions={Permission("server:teleport")},
        )
    assert uow.commits == 0


async def test_create_role_rejects_platform_admin_permission() -> None:
    uow = FakeAuthzUnitOfWork()
    with pytest.raises(UnknownPermissionError):
        await CreateRole(uow=uow, clock=_FakeClock())(
            community_id=CommunityId.new(),
            actor_id=UserId(uuid.uuid4()),
            name="Bad",
            permissions={Permission("worker:manage")},
        )
    assert uow.commits == 0


async def test_update_role_replaces_name_and_permissions() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    actor = UserId(uuid.uuid4())
    uow.add_role(actor, community, {Permission("server:stop")})
    role_id = _seed_custom_role(uow, community)
    role = await UpdateRole(uow=uow, clock=_FakeClock())(
        community_id=community,
        role_id=role_id,
        actor_id=actor,
        name="Operator",
        permissions={Permission("server:stop")},
    )
    assert role.name == RoleName("Operator")
    assert role.permissions == {Permission("server:stop")}


class _RevokeOnReadRoleRepository(FakeRoleRepository):
    """Commits a concurrent revocation right after a read hands the role out."""

    def __init__(self, revoked: Permission) -> None:
        super().__init__()
        self._revoked = revoked

    async def get_by_id(self, role_id: RoleId) -> Role | None:
        role = await super().get_by_id(role_id)
        stored = self.by_id[role_id]
        stored.permissions = stored.permissions - {self._revoked}
        return role


async def test_update_role_rename_keeps_a_revocation_committed_after_its_read() -> None:
    # The rename writes only the name (#3215): it neither restores the permission
    # revoked since its read nor reports the stale set back.
    uow = FakeAuthzUnitOfWork()
    uow.roles = _RevokeOnReadRoleRepository(Permission("server:delete"))
    community = CommunityId.new()
    _seed_community(uow, community)
    role_id = _seed_custom_role(
        uow,
        community,
        permissions={Permission("server:start"), Permission("server:delete")},
    )
    role = await UpdateRole(uow=uow, clock=_FakeClock())(
        community_id=community,
        role_id=role_id,
        actor_id=UserId(uuid.uuid4()),
        name="Operators",
    )
    stored = uow.roles.by_id[role_id]
    assert stored.name == RoleName("Operators")
    assert stored.permissions == {Permission("server:start")}
    assert role.permissions == {Permission("server:start")}


async def test_update_role_rejects_editing_the_preset_owner_role() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    _seed_community(uow, community)
    owner_id = _seed_owner_role(uow, community)
    with pytest.raises(PresetRoleNotEditableError):
        await UpdateRole(uow=uow, clock=_FakeClock())(
            community_id=community,
            role_id=owner_id,
            actor_id=UserId(uuid.uuid4()),
            permissions={Permission("server:read")},
        )


async def test_update_role_rejects_unknown_permission() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    _seed_community(uow, community)
    role_id = _seed_custom_role(uow, community)
    with pytest.raises(UnknownPermissionError):
        await UpdateRole(uow=uow, clock=_FakeClock())(
            community_id=community,
            role_id=role_id,
            actor_id=UserId(uuid.uuid4()),
            permissions={Permission("server:teleport")},
        )


async def test_update_role_rename_in_a_deleted_community_is_not_found() -> None:
    # A rename holds its community first, and a deletion took it (#4021).
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    role_id = _seed_custom_role(uow, community)
    with pytest.raises(CommunityNotFoundError):
        await UpdateRole(uow=uow, clock=_FakeClock())(
            community_id=community,
            role_id=role_id,
            actor_id=UserId(uuid.uuid4()),
            name="X",
        )
    assert uow.roles.by_id[role_id].name == RoleName("Editor")


async def test_update_role_in_other_community_is_not_found() -> None:
    uow = FakeAuthzUnitOfWork()
    role_id = _seed_custom_role(uow, CommunityId.new())
    other = CommunityId.new()
    _seed_community(uow, other)
    with pytest.raises(RoleNotFoundError):
        await UpdateRole(uow=uow, clock=_FakeClock())(
            community_id=other,
            role_id=role_id,
            actor_id=UserId(uuid.uuid4()),
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


class _DeleteOnReadRoleRepository(FakeRoleRepository):
    """Commits a concurrent deletion right after a read hands the role out."""

    async def get_by_id(self, role_id: RoleId) -> Role | None:
        role = await super().get_by_id(role_id)
        self.by_id.pop(role_id, None)
        return role


async def test_delete_role_deleted_since_its_read_is_not_found() -> None:
    # The DELETE matches nothing, which is the role's 404, not a 204 (#4033).
    uow = FakeAuthzUnitOfWork()
    uow.roles = _DeleteOnReadRoleRepository()
    community = CommunityId.new()
    role_id = _seed_custom_role(uow, community)
    with pytest.raises(RoleNotFoundError):
        await DeleteRole(uow=uow)(community_id=community, role_id=role_id)
    assert uow.commits == 0


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
