"""The authoritative operation-permission catalog (REQUIREMENTS.md Appendix A).

The codes here are the single source of truth for authorization (FR-AUTHZ-3):
role and grant editing validate against them, and the ``PermissionChecker``
checks requested operations against them. Codes split along two axes:

- :data:`COMMUNITY_PERMISSIONS` — operations evaluated *within* a Community, via
  the member's roles and resource grants (Layer-2; FR-AUTHZ-2).
- :data:`PLATFORM_ADMIN_PERMISSIONS` — the platform-administrator axis evaluated
  *outside* any Community on the user's ``is_platform_admin`` flag (FR-AUTHZ-5).

Standard-library only: this is pure domain data the rest of the context depends
on.
"""

from __future__ import annotations

from mc_server_dashboard_api.community.domain.errors import UnknownPermissionError
from mc_server_dashboard_api.community.domain.value_objects import Permission

# Community-scoped operation codes (Appendix A, all rows except the admin axis).
COMMUNITY_PERMISSIONS: frozenset[Permission] = frozenset(
    Permission(code)
    for code in (
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
    )
)

# Platform-administrator axis (Appendix A, "Platform (admin axis)" row).
PLATFORM_ADMIN_PERMISSIONS: frozenset[Permission] = frozenset(
    Permission(code)
    for code in (
        "worker:manage",
        "community:provision",
        "platform:monitor",
    )
)

# The whole catalog: the union the membership checks validate against.
ALL_PERMISSIONS: frozenset[Permission] = (
    COMMUNITY_PERMISSIONS | PLATFORM_ADMIN_PERMISSIONS
)

# The seeded preset role granting every community-scoped permission (FR-COMM-4).
# Its permission set is derived from :data:`COMMUNITY_PERMISSIONS`, never a
# hand-written list, so the catalog stays the single source of truth.
OWNER_ROLE_NAME = "Owner"


def is_known_permission(permission: Permission) -> bool:
    """Return whether ``permission`` is in the authoritative catalog."""

    return permission in ALL_PERMISSIONS


def is_platform_admin_permission(permission: Permission) -> bool:
    """Return whether ``permission`` belongs to the platform-admin axis."""

    return permission in PLATFORM_ADMIN_PERMISSIONS


def require_known_permission(permission: Permission) -> Permission:
    """Return ``permission`` if catalogued, else raise ``UnknownPermissionError``."""

    if permission not in ALL_PERMISSIONS:
        raise UnknownPermissionError(permission.value)
    return permission
