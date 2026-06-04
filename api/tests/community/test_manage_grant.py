"""Unit tests for the resource-grant use cases (FR-AUTHZ-2, issue #71).

Against the in-memory fakes (TESTING.md Section 4). The authorization gate lives
in the route dependency, so these verify only the data behaviour: create validates
the target is a member, the resource type is known, and the permissions are valid
for that resource type; revoke rejects a grant from another community as not-found
(cross-community safety, FR-AUTHZ-4). Resource *existence* is not validated —
servers do not exist yet (shape only, per issue #71).
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.community.application.manage_grant import (
    CreateGrant,
    ListGrants,
    RevokeGrant,
)
from mc_server_dashboard_api.community.domain.clock import Clock
from mc_server_dashboard_api.community.domain.entities import Membership
from mc_server_dashboard_api.community.domain.errors import (
    GrantTargetNotMemberError,
    InvalidGrantResourceTypeError,
    ResourceGrantNotFoundError,
    UnknownPermissionError,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    MembershipId,
    Permission,
    ResourceGrantId,
    UserId,
)
from tests.community.fakes import FakeAuthzUnitOfWork

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


class _FakeClock(Clock):
    def now(self) -> dt.datetime:
        return _NOW


def _seed_member(
    uow: FakeAuthzUnitOfWork, user_id: UserId, community_id: CommunityId
) -> None:
    uow.memberships.by_id[MembershipId.new()] = Membership(
        id=MembershipId.new(),
        user_id=user_id,
        community_id=community_id,
        created_at=_NOW,
    )


async def test_create_grant_persists_a_grant_for_a_member() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    user = UserId(uuid.uuid4())
    _seed_member(uow, user, community)
    grant = await CreateGrant(uow=uow, clock=_FakeClock())(
        community_id=community,
        user_id=user,
        resource_type="server",
        resource_id=uuid.uuid4(),
        permissions={Permission("server:start"), Permission("server:stop")},
    )
    assert grant.user_id == user
    assert uow.resource_grants.by_id[grant.id].resource_type == "server"
    assert uow.commits == 1


async def test_create_grant_rejects_non_member_target() -> None:
    uow = FakeAuthzUnitOfWork()
    with pytest.raises(GrantTargetNotMemberError):
        await CreateGrant(uow=uow, clock=_FakeClock())(
            community_id=CommunityId.new(),
            user_id=UserId(uuid.uuid4()),
            resource_type="server",
            resource_id=uuid.uuid4(),
            permissions={Permission("server:start")},
        )
    assert uow.commits == 0


async def test_create_grant_rejects_unknown_resource_type() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    user = UserId(uuid.uuid4())
    _seed_member(uow, user, community)
    with pytest.raises(InvalidGrantResourceTypeError):
        await CreateGrant(uow=uow, clock=_FakeClock())(
            community_id=community,
            user_id=user,
            resource_type="widget",
            resource_id=uuid.uuid4(),
            permissions={Permission("server:start")},
        )


async def test_create_grant_rejects_community_wide_permission() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    user = UserId(uuid.uuid4())
    _seed_member(uow, user, community)
    with pytest.raises(UnknownPermissionError):
        await CreateGrant(uow=uow, clock=_FakeClock())(
            community_id=community,
            user_id=user,
            resource_type="server",
            resource_id=uuid.uuid4(),
            permissions={Permission("member:add")},
        )


async def test_revoke_grant_deletes_the_grant() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    user = UserId(uuid.uuid4())
    grant_id = uow.add_grant(
        user, community, "server", uuid.uuid4(), {Permission("server:start")}
    )
    await RevokeGrant(uow=uow)(community_id=community, grant_id=grant_id)
    assert grant_id not in uow.resource_grants.by_id


async def test_revoke_grant_in_other_community_is_not_found() -> None:
    uow = FakeAuthzUnitOfWork()
    grant_id = uow.add_grant(
        UserId(uuid.uuid4()),
        CommunityId.new(),
        "server",
        uuid.uuid4(),
        {Permission("server:start")},
    )
    with pytest.raises(ResourceGrantNotFoundError):
        await RevokeGrant(uow=uow)(community_id=CommunityId.new(), grant_id=grant_id)


async def test_revoke_unknown_grant_is_not_found() -> None:
    uow = FakeAuthzUnitOfWork()
    with pytest.raises(ResourceGrantNotFoundError):
        await RevokeGrant(uow=uow)(
            community_id=CommunityId.new(), grant_id=ResourceGrantId.new()
        )


async def test_list_grants_scopes_to_community_and_optional_user() -> None:
    uow = FakeAuthzUnitOfWork()
    community = CommunityId.new()
    alice = UserId(uuid.uuid4())
    bob = UserId(uuid.uuid4())
    uow.add_grant(
        alice, community, "server", uuid.uuid4(), {Permission("server:start")}
    )
    uow.add_grant(bob, community, "server", uuid.uuid4(), {Permission("server:stop")})
    uow.add_grant(
        alice, CommunityId.new(), "server", uuid.uuid4(), {Permission("server:read")}
    )

    everyone = await ListGrants(uow=uow)(community_id=community)
    assert len(everyone) == 2

    just_alice = await ListGrants(uow=uow)(community_id=community, user_id=alice)
    assert {g.user_id for g in just_alice} == {alice}
