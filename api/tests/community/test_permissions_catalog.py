"""Unit tests for the authoritative operation-permission catalog (Appendix A).

The catalog is the single source of truth the role/grant validation and the
``PermissionChecker`` reference (FR-AUTHZ-3). These tests pin its contents to
REQUIREMENTS.md Appendix A and the community-scoped versus platform-admin axis
split (FR-AUTHZ-5).
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.community.domain.errors import UnknownPermissionError
from mc_server_dashboard_api.community.domain.permissions import (
    COMMUNITY_PERMISSIONS,
    GRANT_PERMISSIONS_BY_RESOURCE_TYPE,
    PLATFORM_ADMIN_PERMISSIONS,
    is_known_permission,
    is_platform_admin_permission,
    require_community_permission,
    require_grant_permission,
    require_known_permission,
)
from mc_server_dashboard_api.community.domain.value_objects import Permission


def test_community_codes_match_appendix_a() -> None:
    assert {p.value for p in COMMUNITY_PERMISSIONS} == {
        "server:create",
        "server:read",
        "server:update",
        "server:delete",
        "server:start",
        "server:stop",
        "server:restart",
        "server:command",
        "file:read",
        "file:edit",
        "file:history",
        "file:rollback",
        "backup:create",
        "backup:read",
        "backup:restore",
        "backup:delete",
        "backup:schedule",
        "member:read",
        "member:add",
        "member:remove",
        "role:read",
        "role:manage",
        "grant:read",
        "grant:manage",
        "community:read",
        "community:update",
        "community:delete",
        "audit:read",
    }


def test_platform_admin_codes_match_appendix_a() -> None:
    assert {p.value for p in PLATFORM_ADMIN_PERMISSIONS} == {
        "worker:manage",
        "community:provision",
        "platform:monitor",
    }


def test_community_and_platform_axes_are_disjoint() -> None:
    assert COMMUNITY_PERMISSIONS.isdisjoint(PLATFORM_ADMIN_PERMISSIONS)


def test_is_known_permission_accepts_catalog_codes() -> None:
    assert is_known_permission(Permission("server:start")) is True
    assert is_known_permission(Permission("worker:manage")) is True


def test_is_known_permission_rejects_well_formed_but_unlisted_code() -> None:
    # Shape-valid but absent from the catalog.
    assert is_known_permission(Permission("server:teleport")) is False


def test_is_platform_admin_permission_distinguishes_the_axis() -> None:
    assert is_platform_admin_permission(Permission("worker:manage")) is True
    assert is_platform_admin_permission(Permission("server:start")) is False


def test_require_known_permission_passes_through_a_catalog_code() -> None:
    perm = Permission("role:manage")
    assert require_known_permission(perm) is perm


def test_require_known_permission_rejects_unlisted_code() -> None:
    with pytest.raises(UnknownPermissionError):
        require_known_permission(Permission("server:teleport"))


def test_require_community_permission_passes_through_a_community_code() -> None:
    perm = Permission("role:manage")
    assert require_community_permission(perm) is perm


def test_require_community_permission_rejects_platform_admin_code() -> None:
    # A platform-admin axis code must never be assignable via a role or grant.
    with pytest.raises(UnknownPermissionError):
        require_community_permission(Permission("worker:manage"))


def test_require_community_permission_rejects_unknown_code() -> None:
    with pytest.raises(UnknownPermissionError):
        require_community_permission(Permission("server:teleport"))


def test_grant_permissions_for_server_are_resource_scoped_families() -> None:
    # A server grant may carry only server / file / backup operation codes.
    assert {p.value for p in GRANT_PERMISSIONS_BY_RESOURCE_TYPE["server"]} == {
        p.value
        for p in COMMUNITY_PERMISSIONS
        if p.value.split(":", 1)[0] in ("server", "file", "backup")
    }


def test_require_grant_permission_accepts_a_server_scoped_code() -> None:
    perm = Permission("server:start")
    assert require_grant_permission(perm, resource_type="server") is perm


def test_require_grant_permission_rejects_community_wide_code_on_server() -> None:
    # member:add is community-wide, not a per-server operation.
    with pytest.raises(UnknownPermissionError):
        require_grant_permission(Permission("member:add"), resource_type="server")


def test_require_grant_permission_rejects_unknown_resource_type() -> None:
    with pytest.raises(UnknownPermissionError):
        require_grant_permission(Permission("server:start"), resource_type="widget")
