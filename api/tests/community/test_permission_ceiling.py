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
- An actor with no membership: every conferral raises ``CommunityNotFoundError``
  when the community is gone too (issue #2967), and still exceeds the ceiling
  when it is not.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable

import pytest

from mc_server_dashboard_api.community.application.manage_grant import CreateGrant
from mc_server_dashboard_api.community.application.manage_membership import AssignRole
from mc_server_dashboard_api.community.application.manage_role import (
    CreateRole,
    UpdateRole,
)
from mc_server_dashboard_api.community.domain.clock import Clock
from mc_server_dashboard_api.community.domain.entities import (
    Community,
    Membership,
    Role,
)
from mc_server_dashboard_api.community.domain.errors import (
    CommunityNotFoundError,
    PermissionCeilingExceededError,
)
from mc_server_dashboard_api.community.domain.permissions import (
    COMMUNITY_PERMISSIONS,
    OWNER_ROLE_NAME,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    CommunityName,
    MembershipId,
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
    uow.roles.seed(role)
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
    uow.roles.seed(role)
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
    uow.roles.seed(role)
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
    uow.roles.seed(role)
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
    uow.roles.seed(owner_role)
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
    uow.roles.seed(role)
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


# --- an actor whose membership is gone (issue #2967) ------------------------
#
# The ceiling is empty either way. When the community is gone too -- its
# deletion took the actor's membership with it -- the conferral answers
# not-found, as the community's foreign key would have; otherwise the actor
# simply holds nothing here.

_START = Permission("server:start")


def _custom_role(community: CommunityId, permissions: set[Permission]) -> Role:
    return Role(
        id=RoleId.new(),
        community_id=community,
        name=RoleName(f"role-{uuid.uuid4()}"),
        permissions=permissions,
        created_at=_NOW,
        updated_at=_NOW,
        is_preset=False,
    )


async def _create_role_by(
    uow: FakeAuthzUnitOfWork, community: CommunityId, actor: UserId
) -> None:
    await CreateRole(uow=uow, clock=_FakeClock())(
        community_id=community, actor_id=actor, name="Starters", permissions={_START}
    )


async def _update_role_by(
    uow: FakeAuthzUnitOfWork, community: CommunityId, actor: UserId
) -> None:
    role = _custom_role(community, {Permission("server:read")})
    uow.roles.seed(role)
    await UpdateRole(uow=uow, clock=_FakeClock())(
        community_id=community,
        role_id=role.id,
        actor_id=actor,
        permissions={Permission("server:read"), _START},
    )


def _seed_recipient(uow: FakeAuthzUnitOfWork, community: CommunityId) -> UserId:
    # Its membership alone, not its community, which the deleted-community case
    # leaves absent.
    recipient = UserId(uuid.uuid4())
    uow.memberships.seed(
        Membership(
            id=MembershipId.new(),
            user_id=recipient,
            community_id=community,
            created_at=_NOW,
        )
    )
    return recipient


async def _assign_role_by(
    uow: FakeAuthzUnitOfWork, community: CommunityId, actor: UserId
) -> None:
    recipient = _seed_recipient(uow, community)
    role = _custom_role(community, {_START})
    uow.roles.seed(role)
    await AssignRole(uow=uow)(
        community_id=community, user_id=recipient, role_id=role.id, actor_id=actor
    )


async def _create_grant_by(
    uow: FakeAuthzUnitOfWork, community: CommunityId, actor: UserId
) -> None:
    recipient = _seed_recipient(uow, community)
    resource_id = uuid.uuid4()
    uow.add_resource(community, "server", resource_id)
    await CreateGrant(uow=uow, clock=_FakeClock())(
        community_id=community,
        actor_id=actor,
        user_id=recipient,
        resource_type="server",
        resource_id=resource_id,
        permissions={_START},
    )


_Conferral = Callable[[FakeAuthzUnitOfWork, CommunityId, UserId], Awaitable[None]]

_CONFERRALS = [
    pytest.param(_create_role_by, id="create-role"),
    pytest.param(_update_role_by, id="update-role"),
    pytest.param(_assign_role_by, id="assign-role"),
    pytest.param(_create_grant_by, id="create-grant"),
]


@pytest.mark.parametrize("conferral", _CONFERRALS)
async def test_conferral_by_a_non_member_of_a_deleted_community_is_not_found(
    conferral: _Conferral,
) -> None:
    uow = FakeAuthzUnitOfWork()

    with pytest.raises(CommunityNotFoundError):
        await conferral(uow, CommunityId.new(), UserId(uuid.uuid4()))
    assert uow.commits == 0


@pytest.mark.parametrize("conferral", _CONFERRALS)
async def test_conferral_by_a_non_member_of_a_live_community_exceeds_the_ceiling(
    conferral: _Conferral,
) -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    uow.communities.seed(
        Community(
            id=community, name=CommunityName("guild"), created_at=_NOW, updated_at=_NOW
        )
    )

    with pytest.raises(PermissionCeilingExceededError):
        await conferral(uow, community, UserId(uuid.uuid4()))
    assert uow.commits == 0
