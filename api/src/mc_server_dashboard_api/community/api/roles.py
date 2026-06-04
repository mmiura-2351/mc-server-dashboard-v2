"""HTTP edge for community role management (Section 6.4).

Routes live under ``/communities/{community_id}/roles`` per the established
convention so ``require_permission`` can read ``community_id`` from the path and
apply the two-layer check (non-member -> 404 with no existence signal,
member-without-permission -> 403; Section 6.4). The router is thin: it resolves
use cases via dependency injection, runs them, and serialises the result.

Permission gating per operation: list/get use ``role:read``;
create/update/delete use ``role:manage``.

Domain errors are translated to HTTP codes here.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from mc_server_dashboard_api.community.application.manage_role import (
    CreateRole,
    DeleteRole,
    ListRoles,
    ReadRole,
    UpdateRole,
)
from mc_server_dashboard_api.community.domain.entities import Role
from mc_server_dashboard_api.community.domain.errors import (
    InvalidPermissionError,
    InvalidRoleNameError,
    PresetRoleNotEditableError,
    RoleAlreadyExistsError,
    RoleNotFoundError,
    UnknownPermissionError,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    Permission,
    RoleId,
)
from mc_server_dashboard_api.dependencies import (
    get_create_role,
    get_delete_role,
    get_list_roles,
    get_read_role,
    get_update_role,
    require_permission,
)

router = APIRouter()


class CreateRoleRequest(BaseModel):
    name: str = Field(min_length=1)
    permissions: list[str]


class UpdateRoleRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    permissions: list[str] | None = None


class RoleResponse(BaseModel):
    """Public view of a community role (DATABASE.md Section 5)."""

    id: str
    name: str
    permissions: list[str]
    is_preset: bool

    @classmethod
    def from_entity(cls, role: Role) -> "RoleResponse":
        return cls(
            id=str(role.id.value),
            name=role.name.value,
            permissions=sorted(perm.value for perm in role.permissions),
            is_preset=role.is_preset,
        )


@router.get("/communities/{community_id}/roles")
async def list_roles(
    community_id: uuid.UUID,
    _authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("role:read")))
    ],
    use_case: Annotated[ListRoles, Depends(get_list_roles)],
) -> list[RoleResponse]:
    roles = await use_case(community_id=CommunityId(community_id))
    return [RoleResponse.from_entity(role) for role in roles]


@router.get("/communities/{community_id}/roles/{role_id}")
async def read_role(
    community_id: uuid.UUID,
    role_id: uuid.UUID,
    _authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("role:read")))
    ],
    use_case: Annotated[ReadRole, Depends(get_read_role)],
) -> RoleResponse:
    try:
        role = await use_case(
            community_id=CommunityId(community_id), role_id=RoleId(role_id)
        )
    except RoleNotFoundError as exc:
        raise _not_found() from exc
    return RoleResponse.from_entity(role)


@router.post(
    "/communities/{community_id}/roles",
    status_code=status.HTTP_201_CREATED,
)
async def create_role(
    community_id: uuid.UUID,
    body: CreateRoleRequest,
    _authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("role:manage")))
    ],
    use_case: Annotated[CreateRole, Depends(get_create_role)],
) -> RoleResponse:
    try:
        role = await use_case(
            community_id=CommunityId(community_id),
            name=body.name,
            permissions=_parse_permissions(body.permissions),
        )
    except RoleAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "name_taken"},
        ) from exc
    except InvalidRoleNameError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": "invalid_name"},
        ) from exc
    except UnknownPermissionError as exc:
        raise _invalid_permission() from exc
    return RoleResponse.from_entity(role)


@router.patch("/communities/{community_id}/roles/{role_id}")
async def update_role(
    community_id: uuid.UUID,
    role_id: uuid.UUID,
    body: UpdateRoleRequest,
    _authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("role:manage")))
    ],
    use_case: Annotated[UpdateRole, Depends(get_update_role)],
) -> RoleResponse:
    try:
        role = await use_case(
            community_id=CommunityId(community_id),
            role_id=RoleId(role_id),
            name=body.name,
            permissions=(
                None
                if body.permissions is None
                else _parse_permissions(body.permissions)
            ),
        )
    except RoleNotFoundError as exc:
        raise _not_found() from exc
    except PresetRoleNotEditableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "preset_role"},
        ) from exc
    except RoleAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "name_taken"},
        ) from exc
    except InvalidRoleNameError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": "invalid_name"},
        ) from exc
    except UnknownPermissionError as exc:
        raise _invalid_permission() from exc
    return RoleResponse.from_entity(role)


@router.delete(
    "/communities/{community_id}/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_role(
    community_id: uuid.UUID,
    role_id: uuid.UUID,
    _authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("role:manage")))
    ],
    use_case: Annotated[DeleteRole, Depends(get_delete_role)],
) -> None:
    try:
        await use_case(community_id=CommunityId(community_id), role_id=RoleId(role_id))
    except RoleNotFoundError as exc:
        raise _not_found() from exc
    except PresetRoleNotEditableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "preset_role"},
        ) from exc


def _parse_permissions(raw: list[str]) -> set[Permission]:
    # Shape (<resource>:<action>) only; catalog validation is the use case's job,
    # surfacing as UnknownPermissionError (caught in the handlers).
    try:
        return {Permission(code) for code in raw}
    except InvalidPermissionError as exc:
        raise _invalid_permission() from exc


def _invalid_permission() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"reason": "invalid_permission"},
    )


def _not_found() -> HTTPException:
    # Keep the no-existence-signal posture (Section 6.4): a role outside this
    # community or a community that vanished concurrently both 404.
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
