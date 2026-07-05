"""Unit tests for the grant-only-what-you-hold ceiling (issue #1595).

Against the in-memory fakes (TESTING.md Section 4). Verifies:

- CreateRole / UpdateRole with permissions outside the actor's ceiling are
  rejected with ``PermissionCeilingExceededError``.
- CreateRole / UpdateRole within the ceiling succeed.
- The actor's resource grant does NOT count toward the ceiling for role
  operations (roles confer community-wide).
- CreateGrant: the actor's own grant on the same resource counts toward the
  ceiling; a grant on a different resource does not.
- AssignRole: assigning a role with permissions the actor lacks is rejected
  (including self-assign of Owner by a role:manage-only actor).
- UpdateRole delta semantics: removing permissions succeeds even when the actor
  lacks the removed codes.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.community.application.manage_grant import CreateGrant
from mc_server_dashboard_api.community.application.manage_membership import AssignRole
from mc_server_dashboard_api.community.application.manage_role import (
    CreateRole,
    UpdateRole,
)
from mc_server_dashboard_api.community.domain.clock import Clock
from mc_server_dashboard_api.community.domain.entities import Role
from mc_server_dashboard_api.community.domain.errors import (
    PermissionCeilingExceededError,
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
    UserId,
)
from tests.community.fakes import FakeAuthzUnitOfWork

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


class _FakeClock(Clock):
    def now(self) -> dt.datetime:
        return _NOW


# --- CreateRole ceiling -----------------------------------------------------


async def test_create_role_outside_actor_ceiling_raises() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    actor = UserId(uuid.uuid4())
    # Actor holds only server:read.
    uow.add_role(actor, community, {Permission("server:read")})
    with pytest.raises(PermissionCeilingExceededError) as exc_info:
        await CreateRole(uow=uow, clock=_FakeClock())(
            community_id=community,
            actor_id=actor,
            name="Escalated",
            permissions={Permission("server:read"), Permission("community:delete")},
        )
    assert "community:delete" in exc_info.value.exceeded
    assert uow.commits == 0


async def test_create_role_within_actor_ceiling_succeeds() -> None:
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
        name="Operator",
        permissions={Permission("server:read")},
    )
    assert role.permissions == {Permission("server:read")}
    assert uow.commits == 1


async def test_create_role_actor_resource_grant_does_not_count() -> None:
    """A resource grant gives per-server access; it must NOT widen the ceiling
    for role operations that confer community-wide permissions."""
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    actor = UserId(uuid.uuid4())
    resource_id = uuid.uuid4()
    # Actor holds server:read via a role, and server:start only via a resource grant.
    uow.add_role(actor, community, {Permission("server:read")})
    uow.add_grant(actor, community, "server", resource_id, {Permission("server:start")})
    with pytest.raises(PermissionCeilingExceededError) as exc_info:
        await CreateRole(uow=uow, clock=_FakeClock())(
            community_id=community,
            actor_id=actor,
            name="Escalated",
            permissions={Permission("server:read"), Permission("server:start")},
        )
    assert "server:start" in exc_info.value.exceeded


# --- UpdateRole ceiling / delta ---------------------------------------------


async def test_update_role_adding_code_outside_ceiling_raises() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    actor = UserId(uuid.uuid4())
    uow.add_role(actor, community, {Permission("server:read")})
    # Seed a custom role with server:read.
    role = Role(
        id=RoleId.new(),
        community_id=community,
        name=RoleName("Editor"),
        permissions={Permission("server:read")},
        created_at=_NOW,
        updated_at=_NOW,
        is_preset=False,
    )
    uow.roles.by_id[role.id] = role
    with pytest.raises(PermissionCeilingExceededError) as exc_info:
        await UpdateRole(uow=uow, clock=_FakeClock())(
            community_id=community,
            role_id=role.id,
            actor_id=actor,
            permissions={Permission("server:read"), Permission("community:delete")},
        )
    assert "community:delete" in exc_info.value.exceeded


async def test_update_role_removing_codes_succeeds_even_if_actor_lacks_them() -> None:
    """Delta semantics: only newly added codes are checked.  Removing a code the
    actor does not hold is fine — it is a restriction, not an escalation."""
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    actor = UserId(uuid.uuid4())
    # Actor holds only server:read.
    uow.add_role(actor, community, {Permission("server:read")})
    # Role currently has server:read + community:delete.
    role = Role(
        id=RoleId.new(),
        community_id=community,
        name=RoleName("Demote"),
        permissions={Permission("server:read"), Permission("community:delete")},
        created_at=_NOW,
        updated_at=_NOW,
        is_preset=False,
    )
    uow.roles.by_id[role.id] = role
    # Remove community:delete (actor does not hold it, but that is OK).
    updated = await UpdateRole(uow=uow, clock=_FakeClock())(
        community_id=community,
        role_id=role.id,
        actor_id=actor,
        permissions={Permission("server:read")},
    )
    assert updated.permissions == {Permission("server:read")}
    assert uow.commits == 1


async def test_update_role_within_ceiling_succeeds() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    actor = UserId(uuid.uuid4())
    uow.add_role(
        actor,
        community,
        {Permission("server:read"), Permission("server:stop")},
    )
    role = Role(
        id=RoleId.new(),
        community_id=community,
        name=RoleName("Tweaked"),
        permissions={Permission("server:read")},
        created_at=_NOW,
        updated_at=_NOW,
        is_preset=False,
    )
    uow.roles.by_id[role.id] = role
    updated = await UpdateRole(uow=uow, clock=_FakeClock())(
        community_id=community,
        role_id=role.id,
        actor_id=actor,
        permissions={Permission("server:read"), Permission("server:stop")},
    )
    assert Permission("server:stop") in updated.permissions


# --- CreateGrant ceiling ----------------------------------------------------


async def test_create_grant_outside_actor_ceiling_raises() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    actor = UserId(uuid.uuid4())
    target = UserId(uuid.uuid4())
    resource_id = uuid.uuid4()
    uow.add_role(actor, community, {Permission("server:read")})
    uow._membership_for(target, community)
    uow.add_resource(community, "server", resource_id)
    with pytest.raises(PermissionCeilingExceededError) as exc_info:
        await CreateGrant(uow=uow, clock=_FakeClock())(
            community_id=community,
            actor_id=actor,
            user_id=target,
            resource_type="server",
            resource_id=resource_id,
            permissions={Permission("server:start")},
        )
    assert "server:start" in exc_info.value.exceeded
    assert uow.commits == 0


async def test_create_grant_actor_grant_on_same_resource_counts() -> None:
    """When creating a grant, the actor's own grant on the same resource adds to
    their ceiling — they already have that permission on that server."""
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    actor = UserId(uuid.uuid4())
    target = UserId(uuid.uuid4())
    resource_id = uuid.uuid4()
    # Actor has server:read via a role, and server:start via a grant on this resource.
    uow.add_role(actor, community, {Permission("server:read")})
    uow.add_grant(actor, community, "server", resource_id, {Permission("server:start")})
    uow._membership_for(target, community)
    uow.add_resource(community, "server", resource_id)
    grant = await CreateGrant(uow=uow, clock=_FakeClock())(
        community_id=community,
        actor_id=actor,
        user_id=target,
        resource_type="server",
        resource_id=resource_id,
        permissions={Permission("server:start")},
    )
    assert grant.permissions == {Permission("server:start")}
    assert uow.commits == 1


async def test_create_grant_actor_grant_on_different_resource_does_not_count() -> None:
    """A grant on server-A does not widen the ceiling for granting on server-B."""
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    actor = UserId(uuid.uuid4())
    target = UserId(uuid.uuid4())
    resource_a = uuid.uuid4()
    resource_b = uuid.uuid4()
    uow.add_role(actor, community, {Permission("server:read")})
    uow.add_grant(actor, community, "server", resource_a, {Permission("server:start")})
    uow._membership_for(target, community)
    uow.add_resource(community, "server", resource_b)
    with pytest.raises(PermissionCeilingExceededError) as exc_info:
        await CreateGrant(uow=uow, clock=_FakeClock())(
            community_id=community,
            actor_id=actor,
            user_id=target,
            resource_type="server",
            resource_id=resource_b,
            permissions={Permission("server:start")},
        )
    assert "server:start" in exc_info.value.exceeded


# --- AssignRole ceiling -----------------------------------------------------


async def test_assign_role_with_permissions_outside_actor_ceiling_raises() -> None:
    """A role:manage-only actor cannot assign a role carrying community:delete."""
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    actor = UserId(uuid.uuid4())
    target = UserId(uuid.uuid4())
    # Actor holds only role:manage.
    uow.add_role(actor, community, {Permission("role:manage")})
    uow._membership_for(target, community)
    # The target role carries community:delete — which actor lacks.
    role = Role(
        id=RoleId.new(),
        community_id=community,
        name=RoleName("Admin"),
        permissions={Permission("community:delete"), Permission("role:manage")},
        created_at=_NOW,
        updated_at=_NOW,
        is_preset=False,
    )
    uow.roles.by_id[role.id] = role
    with pytest.raises(PermissionCeilingExceededError) as exc_info:
        await AssignRole(uow=uow)(
            community_id=community,
            user_id=target,
            role_id=role.id,
            actor_id=actor,
        )
    assert "community:delete" in exc_info.value.exceeded


async def test_assign_owner_role_by_role_manage_only_actor_is_rejected() -> None:
    """A ``role:manage``-only actor cannot assign the Owner role (self or other)."""
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    actor = UserId(uuid.uuid4())
    uow.add_role(actor, community, {Permission("role:manage")})
    # Seed the preset Owner role with the full community-scoped catalog.
    owner_role = Role(
        id=RoleId.new(),
        community_id=community,
        name=RoleName(OWNER_ROLE_NAME),
        permissions=set(COMMUNITY_PERMISSIONS),
        created_at=_NOW,
        updated_at=_NOW,
        is_preset=True,
    )
    uow.roles.by_id[owner_role.id] = owner_role
    with pytest.raises(PermissionCeilingExceededError):
        await AssignRole(uow=uow)(
            community_id=community,
            user_id=actor,  # self-assign
            role_id=owner_role.id,
            actor_id=actor,
        )


async def test_assign_role_within_ceiling_succeeds() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    actor = UserId(uuid.uuid4())
    target = UserId(uuid.uuid4())
    uow.add_role(
        actor,
        community,
        {Permission("server:read"), Permission("role:manage")},
    )
    uow._membership_for(target, community)
    role = Role(
        id=RoleId.new(),
        community_id=community,
        name=RoleName("Viewer"),
        permissions={Permission("server:read")},
        created_at=_NOW,
        updated_at=_NOW,
        is_preset=False,
    )
    uow.roles.by_id[role.id] = role
    await AssignRole(uow=uow)(
        community_id=community,
        user_id=target,
        role_id=role.id,
        actor_id=actor,
    )
    target_membership = await uow.memberships.get_by_user_and_community(
        target, community
    )
    assert target_membership is not None
    assert role.id in await uow.memberships.list_role_ids(target_membership.id)
