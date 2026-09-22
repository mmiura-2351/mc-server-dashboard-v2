"""Unit tests for the community value objects (pure, no I/O)."""

import pytest

from mc_server_dashboard_api.community.domain.errors import (
    InvalidCommunityNameError,
    InvalidPermissionError,
    InvalidRoleNameError,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityName,
    Permission,
    RoleName,
)


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
