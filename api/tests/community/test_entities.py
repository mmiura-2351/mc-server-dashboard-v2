"""Unit tests for the community entities (pure, no I/O)."""

import datetime as dt
import uuid

from mc_server_dashboard_api.community.domain.entities import (
    Community,
    Membership,
    ResourceGrant,
    Role,
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

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


def test_community_quota_columns_default_to_none() -> None:
    # DATABASE.md Section 5: max_servers/max_members are nullable, unused in M1.
    community = Community(
        id=CommunityId.new(),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    assert community.max_servers is None
    assert community.max_members is None


def test_community_can_carry_quota_values() -> None:
    community = Community(
        id=CommunityId.new(),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
        max_servers=5,
        max_members=20,
    )
    assert community.max_servers == 5
    assert community.max_members == 20


def test_membership_carries_user_and_community_ids() -> None:
    user_id = UserId(uuid.uuid4())
    community_id = CommunityId.new()
    membership = Membership(
        id=MembershipId.new(),
        user_id=user_id,
        community_id=community_id,
        created_at=_NOW,
    )
    assert membership.user_id == user_id
    assert membership.community_id == community_id


def test_role_defaults_to_non_preset() -> None:
    role = Role(
        id=RoleId.new(),
        community_id=CommunityId.new(),
        name=RoleName("Owner"),
        permissions={Permission("server:start")},
        created_at=_NOW,
        updated_at=_NOW,
    )
    assert role.is_preset is False
    assert Permission("server:start") in role.permissions


def test_role_can_be_marked_preset() -> None:
    role = Role(
        id=RoleId.new(),
        community_id=CommunityId.new(),
        name=RoleName("Owner"),
        permissions=set(),
        created_at=_NOW,
        updated_at=_NOW,
        is_preset=True,
    )
    assert role.is_preset is True


def test_resource_grant_carries_soft_resource_reference() -> None:
    resource_id = uuid.uuid4()
    grant = ResourceGrant(
        id=ResourceGrantId.new(),
        user_id=UserId(uuid.uuid4()),
        community_id=CommunityId.new(),
        resource_type="server",
        resource_id=resource_id,
        permissions={Permission("server:stop")},
        created_at=_NOW,
        updated_at=_NOW,
    )
    assert grant.resource_type == "server"
    assert grant.resource_id == resource_id
    assert Permission("server:stop") in grant.permissions
