"""Unit tests for the community value objects (pure, no I/O)."""

import uuid

import pytest

from mc_server_dashboard_api.community.domain.errors import (
    InvalidCommunityNameError,
    InvalidPermissionError,
    InvalidRoleNameError,
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


def test_ids_new_is_unique() -> None:
    assert CommunityId.new() != CommunityId.new()
    assert MembershipId.new() != MembershipId.new()
    assert RoleId.new() != RoleId.new()
    assert ResourceGrantId.new() != ResourceGrantId.new()


def test_ids_wrap_a_uuid() -> None:
    raw = uuid.uuid4()
    assert CommunityId(raw).value == raw
    assert MembershipId(raw).value == raw
    assert RoleId(raw).value == raw
    assert ResourceGrantId(raw).value == raw


def test_user_id_is_a_plain_uuid_reference() -> None:
    raw = uuid.uuid4()
    assert UserId(raw).value == raw


def test_community_name_must_not_be_blank() -> None:
    with pytest.raises(InvalidCommunityNameError):
        CommunityName("   ")


def test_community_name_trims_surrounding_whitespace() -> None:
    assert CommunityName("  guild  ").value == "guild"


def test_role_name_must_not_be_blank() -> None:
    with pytest.raises(InvalidRoleNameError):
        RoleName("  ")


def test_role_name_trims_surrounding_whitespace() -> None:
    assert RoleName("  Owner  ").value == "Owner"


def test_permission_accepts_resource_action_shape() -> None:
    assert Permission("server:start").value == "server:start"


@pytest.mark.parametrize(
    "code", ["server", "server:", ":start", "serverstart", "  server:start  "]
)
def test_permission_rejects_malformed_codes(code: str) -> None:
    with pytest.raises(InvalidPermissionError):
        Permission(code)


def test_permission_rejects_extra_colon() -> None:
    with pytest.raises(InvalidPermissionError):
        Permission("server:start:now")


def test_permission_equality_and_hashing_by_value() -> None:
    assert Permission("server:start") == Permission("server:start")
    assert len({Permission("server:start"), Permission("server:start")}) == 1
