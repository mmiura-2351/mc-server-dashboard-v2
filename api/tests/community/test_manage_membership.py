"""Unit tests for the membership use cases (FR-MEM-1/2/3, role assignment).

Against the in-memory fakes (TESTING.md Section 4). The authorization gate lives
in the route dependency, so these verify only the data behaviour: add validates
the user and stages a membership; remove sweeps grants in this community while
leaving other communities' grants untouched (FR-MEM-2/3), and refuses to remove
the last Owner-role holder; list returns members with their role names; role
assign/unassign validate the role belongs to *this* community (cross-community
assignment must fail — FR-AUTHZ-4).
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.community.application.manage_membership import (
    AddMember,
    AssignRole,
    ListMembers,
    RemoveMember,
    UnassignRole,
)
from mc_server_dashboard_api.community.domain.clock import Clock
from mc_server_dashboard_api.community.domain.entities import Community, Role
from mc_server_dashboard_api.community.domain.errors import (
    CommunityNotFoundError,
    LastOwnerRemovalError,
    MembershipNotFoundError,
    MemberUserNotFoundError,
    RoleNotFoundError,
)
from mc_server_dashboard_api.community.domain.permissions import OWNER_ROLE_NAME
from mc_server_dashboard_api.community.domain.user_directory import UserDirectory
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    CommunityName,
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


class _FakeUserDirectory(UserDirectory):
    def __init__(self, *, known: bool) -> None:
        self._known = known

    async def exists(self, user_id: UserId) -> bool:
        return self._known


def _seed_community(uow: FakeAuthzUnitOfWork, name: str = "guild") -> Community:
    community = Community(
        id=CommunityId.new(),
        name=CommunityName(name),
        created_at=_NOW,
        updated_at=_NOW,
    )
    uow.communities.by_id[community.id] = community
    return community


def _seed_owner_role(uow: FakeAuthzUnitOfWork, community_id: CommunityId) -> Role:
    role = Role(
        id=RoleId.new(),
        community_id=community_id,
        name=RoleName(OWNER_ROLE_NAME),
        permissions=set(),
        created_at=_NOW,
        updated_at=_NOW,
        is_preset=True,
    )
    uow.roles.by_id[role.id] = role
    return role


# --- AddMember --------------------------------------------------------------


async def test_add_member_stages_membership_and_commits() -> None:
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow)
    user = UserId(uuid.uuid4())

    membership = await AddMember(
        uow=uow, users=_FakeUserDirectory(known=True), clock=_FakeClock()
    )(community_id=community.id, user_id=user)

    assert membership.user_id == user
    assert membership.community_id == community.id
    persisted = await uow.memberships.get_by_user_and_community(user, community.id)
    assert persisted is not None
    assert uow.commits == 1


async def test_add_member_unknown_user_is_rejected_and_not_committed() -> None:
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow)
    with pytest.raises(MemberUserNotFoundError):
        await AddMember(
            uow=uow, users=_FakeUserDirectory(known=False), clock=_FakeClock()
        )(community_id=community.id, user_id=UserId(uuid.uuid4()))
    assert uow.commits == 0


# --- RemoveMember -----------------------------------------------------------


async def test_remove_member_deletes_membership_and_sweeps_grants() -> None:
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow)
    user = UserId(uuid.uuid4())
    uow.add_role(user, community.id, {Permission("server:read")})
    uow.add_grant(
        user, community.id, "server", uuid.uuid4(), {Permission("server:start")}
    )

    await RemoveMember(uow=uow)(community_id=community.id, user_id=user)

    assert await uow.memberships.get_by_user_and_community(user, community.id) is None
    assert uow.resource_grants.by_id == {}
    assert uow.commits == 1


async def test_remove_member_keeps_grants_in_other_communities() -> None:
    # FR-MEM-2/FR-MEM-3 scoping: removing the user from B leaves A's grants intact.
    uow = FakeAuthzUnitOfWork()
    community_a = _seed_community(uow, "a")
    community_b = _seed_community(uow, "b")
    user = UserId(uuid.uuid4())
    uow.add_role(user, community_a.id, {Permission("server:read")})
    uow.add_role(user, community_b.id, {Permission("server:read")})
    grant_a = uow.add_grant(
        user, community_a.id, "server", uuid.uuid4(), {Permission("server:start")}
    )
    uow.add_grant(
        user, community_b.id, "server", uuid.uuid4(), {Permission("server:start")}
    )

    await RemoveMember(uow=uow)(community_id=community_b.id, user_id=user)

    # The B membership and grant are gone; the A ones survive.
    assert await uow.memberships.get_by_user_and_community(user, community_b.id) is None
    assert (
        await uow.memberships.get_by_user_and_community(user, community_a.id)
        is not None
    )
    remaining = set(uow.resource_grants.by_id)
    assert remaining == {grant_a}


async def test_remove_non_member_raises_not_found() -> None:
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow)
    with pytest.raises(MembershipNotFoundError):
        await RemoveMember(uow=uow)(
            community_id=community.id, user_id=UserId(uuid.uuid4())
        )


async def test_remove_last_owner_is_rejected() -> None:
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow)
    owner_role = _seed_owner_role(uow, community.id)
    owner = UserId(uuid.uuid4())
    membership = uow._membership_for(owner, community.id)
    uow.memberships.role_ids.setdefault(membership.id, []).append(owner_role.id)

    with pytest.raises(LastOwnerRemovalError):
        await RemoveMember(uow=uow)(community_id=community.id, user_id=owner)
    # Nothing removed/committed.
    assert (
        await uow.memberships.get_by_user_and_community(owner, community.id) is not None
    )
    assert uow.commits == 0


async def test_remove_owner_allowed_when_another_owner_remains() -> None:
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow)
    owner_role = _seed_owner_role(uow, community.id)
    first = UserId(uuid.uuid4())
    second = UserId(uuid.uuid4())
    for user in (first, second):
        membership = uow._membership_for(user, community.id)
        uow.memberships.role_ids.setdefault(membership.id, []).append(owner_role.id)

    await RemoveMember(uow=uow)(community_id=community.id, user_id=first)

    assert await uow.memberships.get_by_user_and_community(first, community.id) is None
    assert (
        await uow.memberships.get_by_user_and_community(second, community.id)
        is not None
    )


async def test_remove_non_owner_is_allowed_even_as_sole_owner_exists() -> None:
    # A non-owner member can always be removed; the last-Owner guard only bites
    # the Owner-role holder.
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow)
    owner_role = _seed_owner_role(uow, community.id)
    owner = UserId(uuid.uuid4())
    plain = UserId(uuid.uuid4())
    owner_membership = uow._membership_for(owner, community.id)
    uow.memberships.role_ids.setdefault(owner_membership.id, []).append(owner_role.id)
    uow._membership_for(plain, community.id)

    await RemoveMember(uow=uow)(community_id=community.id, user_id=plain)

    assert await uow.memberships.get_by_user_and_community(plain, community.id) is None


# --- ListMembers ------------------------------------------------------------


async def test_list_members_returns_members_with_role_names() -> None:
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow)
    user = UserId(uuid.uuid4())
    uow.add_role(user, community.id, {Permission("server:read")}, name="Editor")

    members = await ListMembers(uow=uow)(community_id=community.id)

    assert len(members) == 1
    assert members[0].user_id == user
    assert members[0].role_names == ["Editor"]


async def test_list_members_missing_community_raises_not_found() -> None:
    uow = FakeAuthzUnitOfWork()
    with pytest.raises(CommunityNotFoundError):
        await ListMembers(uow=uow)(community_id=CommunityId(uuid.uuid4()))


# --- AssignRole / UnassignRole ---------------------------------------------


async def test_assign_role_attaches_role_to_member() -> None:
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow)
    user = UserId(uuid.uuid4())
    membership = uow._membership_for(user, community.id)
    role = _seed_owner_role(uow, community.id)

    await AssignRole(uow=uow)(community_id=community.id, user_id=user, role_id=role.id)

    assert await uow.memberships.list_role_ids(membership.id) == [role.id]
    assert uow.commits == 1


async def test_assign_role_is_idempotent() -> None:
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow)
    user = UserId(uuid.uuid4())
    membership = uow._membership_for(user, community.id)
    role = _seed_owner_role(uow, community.id)
    uow.memberships.role_ids.setdefault(membership.id, []).append(role.id)

    await AssignRole(uow=uow)(community_id=community.id, user_id=user, role_id=role.id)

    assert await uow.memberships.list_role_ids(membership.id) == [role.id]
    assert uow.commits == 0


async def test_assign_role_to_non_member_raises_not_found() -> None:
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow)
    role = _seed_owner_role(uow, community.id)
    with pytest.raises(MembershipNotFoundError):
        await AssignRole(uow=uow)(
            community_id=community.id, user_id=UserId(uuid.uuid4()), role_id=role.id
        )


async def test_assign_unknown_role_raises_not_found() -> None:
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow)
    user = UserId(uuid.uuid4())
    uow._membership_for(user, community.id)
    with pytest.raises(RoleNotFoundError):
        await AssignRole(uow=uow)(
            community_id=community.id, user_id=user, role_id=RoleId(uuid.uuid4())
        )


async def test_assign_role_from_another_community_fails() -> None:
    # Security-critical: the membership_role FK accepts any role id, so the use
    # case must reject a role belonging to a different community (FR-AUTHZ-4).
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow, "mine")
    other = _seed_community(uow, "other")
    foreign_role = _seed_owner_role(uow, other.id)
    user = UserId(uuid.uuid4())
    membership = uow._membership_for(user, community.id)

    with pytest.raises(RoleNotFoundError):
        await AssignRole(uow=uow)(
            community_id=community.id, user_id=user, role_id=foreign_role.id
        )
    assert await uow.memberships.list_role_ids(membership.id) == []


async def test_unassign_role_detaches_role() -> None:
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow)
    user = UserId(uuid.uuid4())
    membership = uow._membership_for(user, community.id)
    role = _seed_owner_role(uow, community.id)
    uow.memberships.role_ids.setdefault(membership.id, []).append(role.id)

    await UnassignRole(uow=uow)(
        community_id=community.id, user_id=user, role_id=role.id
    )

    assert await uow.memberships.list_role_ids(membership.id) == []
    assert uow.commits == 1


async def test_unassign_role_from_another_community_fails() -> None:
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow, "mine")
    other = _seed_community(uow, "other")
    foreign_role = _seed_owner_role(uow, other.id)
    user = UserId(uuid.uuid4())
    uow._membership_for(user, community.id)

    with pytest.raises(RoleNotFoundError):
        await UnassignRole(uow=uow)(
            community_id=community.id, user_id=user, role_id=foreign_role.id
        )
