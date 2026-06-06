"""HTTP edge for the caller's own community self-service (Section 6.4, issue #354).

``GET /communities/{community_id}/me/permissions`` lets an ordinary member read
their *own* effective permission set so the UI can scope its controls. Server-side
enforcement stays authoritative (FR-AUTHZ-6); this is rendering convenience only.

Unlike the other community routes, this one gates on Layer-1 membership *only*: a
member may always read their own set, so there is no per-operation permission to
require (requiring ``member:read`` / ``role:read`` / ``grant:read`` would defeat
the purpose — a low-privilege member typically holds none of them). A non-member
still gets 404 with no existence signal (FR-COMM-3), so the route runs the same
visibility primitive ``require_permission`` runs first, just without the Layer-2
``can`` check.

The router is thin: it resolves the use case via dependency injection, runs it,
and serialises the result.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from mc_server_dashboard_api.community.application.read_my_permissions import (
    EffectivePermissions,
    ReadMyEffectivePermissions,
)
from mc_server_dashboard_api.community.domain.entities import ResourceGrant
from mc_server_dashboard_api.community.domain.permission_checker import (
    MembershipVisibility,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    UserId,
)
from mc_server_dashboard_api.dependencies import (
    get_current_user,
    get_membership_visibility,
    get_read_my_effective_permissions,
)
from mc_server_dashboard_api.identity.domain.entities import User

router = APIRouter()


class GrantPermissionsResponse(BaseModel):
    """The caller's own grant on one resource (DATABASE.md Section 6)."""

    resource_type: str
    resource_id: str
    permissions: list[str]

    @classmethod
    def from_entity(cls, grant: ResourceGrant) -> "GrantPermissionsResponse":
        return cls(
            resource_type=grant.resource_type,
            resource_id=str(grant.resource_id),
            permissions=sorted(perm.value for perm in grant.permissions),
        )


class EffectivePermissionsResponse(BaseModel):
    """The caller's community-wide codes and per-resource grants (issue #354)."""

    permissions: list[str]
    grants: list[GrantPermissionsResponse]

    @classmethod
    def from_result(
        cls, result: EffectivePermissions
    ) -> "EffectivePermissionsResponse":
        return cls(
            permissions=sorted(perm.value for perm in result.permissions),
            grants=[GrantPermissionsResponse.from_entity(g) for g in result.grants],
        )


@router.get("/communities/{community_id}/me/permissions")
async def read_my_permissions(
    community_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    visibility: Annotated[MembershipVisibility, Depends(get_membership_visibility)],
    use_case: Annotated[
        ReadMyEffectivePermissions, Depends(get_read_my_effective_permissions)
    ],
) -> EffectivePermissionsResponse:
    community = CommunityId(community_id)
    auth_user = AuthUser(
        user_id=UserId(user.id.value),
        is_platform_admin=user.is_platform_admin,
    )
    if not await visibility.is_member(
        user_id=auth_user.user_id, community_id=community
    ):
        # Layer-1: a non-member gets no existence signal (FR-COMM-3, Section 6.4).
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    result = await use_case(user=auth_user, community_id=community)
    return EffectivePermissionsResponse.from_result(result)
