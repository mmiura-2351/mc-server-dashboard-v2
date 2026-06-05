"""Platform-admin user-administration endpoints (M2 Epic A2, issue #278).

The cross-cutting admin axis (FR-AUTHZ-5) gates these, the same posture as the
/workers surface: every route depends on ``require_platform_admin`` (non-admin ->
403). They cover the user lifecycle an admin owns -- list, deactivate /
reactivate, delete, and grant / revoke the platform-admin flag -- and audit each
action (actor = the admin, target = the affected user).

The refusals mirror the self-service routes' semantics: a community owner or the
last active platform admin cannot be deleted (409), and an admin cannot
deactivate or delete *themselves* through these routes (409, ``self_target``) --
they use ``/users/me`` for that. Each conflicting state carries a stable
machine-readable ``reason``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.dependencies import (
    get_admin_delete_user,
    get_audit_recorder,
    get_list_users,
    get_set_platform_admin,
    get_set_user_active,
    require_platform_admin,
)
from mc_server_dashboard_api.identity.application.admin_delete_user import (
    AdminDeleteUser,
)
from mc_server_dashboard_api.identity.application.list_users import ListUsers
from mc_server_dashboard_api.identity.application.set_platform_admin import (
    SetPlatformAdmin,
)
from mc_server_dashboard_api.identity.application.set_user_active import SetUserActive
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import (
    CommunityOwnedError,
    LastPlatformAdminError,
    SelfTargetError,
    UserNotFoundError,
)
from mc_server_dashboard_api.identity.domain.value_objects import UserId

router = APIRouter()


class AdminUserResponse(BaseModel):
    """Admin view of a user (adds ``active`` and ``created_at`` to the public view)."""

    id: str
    username: str
    email: str
    is_platform_admin: bool
    active: bool
    created_at: str

    @classmethod
    def from_entity(cls, user: User) -> "AdminUserResponse":
        return cls(
            id=str(user.id.value),
            username=user.username.value,
            email=user.email.value,
            is_platform_admin=user.is_platform_admin,
            active=user.active,
            created_at=user.created_at.isoformat(),
        )


class UserListResponse(BaseModel):
    users: list[AdminUserResponse]
    total: int
    limit: int
    offset: int


class PlatformAdminRequest(BaseModel):
    grant: bool


@router.get("/users", dependencies=[Depends(require_platform_admin)])
async def list_users(
    use_case: Annotated[ListUsers, Depends(get_list_users)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UserListResponse:
    page = await use_case(limit=limit, offset=offset)
    return UserListResponse(
        users=[AdminUserResponse.from_entity(u) for u in page.users],
        total=page.total,
        limit=limit,
        offset=offset,
    )


@router.post("/users/{user_id}/deactivate", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_user(
    user_id: uuid.UUID,
    admin: Annotated[User, Depends(require_platform_admin)],
    use_case: Annotated[SetUserActive, Depends(get_set_user_active)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> Response:
    try:
        await use_case(actor_id=admin.id, target_id=UserId(user_id), active=False)
    except SelfTargetError as exc:
        raise _conflict("self_target") from exc
    except LastPlatformAdminError as exc:
        raise _conflict("last_platform_admin") from exc
    except UserNotFoundError as exc:
        raise _not_found() from exc
    await _audit(recorder, ops.USER_DEACTIVATE, admin=admin, target=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/users/{user_id}/reactivate", status_code=status.HTTP_204_NO_CONTENT)
async def reactivate_user(
    user_id: uuid.UUID,
    admin: Annotated[User, Depends(require_platform_admin)],
    use_case: Annotated[SetUserActive, Depends(get_set_user_active)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> Response:
    try:
        await use_case(actor_id=admin.id, target_id=UserId(user_id), active=True)
    except UserNotFoundError as exc:
        raise _not_found() from exc
    await _audit(recorder, ops.USER_REACTIVATE, admin=admin, target=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    admin: Annotated[User, Depends(require_platform_admin)],
    use_case: Annotated[AdminDeleteUser, Depends(get_admin_delete_user)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> Response:
    try:
        await use_case(actor_id=admin.id, target_id=UserId(user_id))
    except SelfTargetError as exc:
        raise _conflict("self_target") from exc
    except CommunityOwnedError as exc:
        raise _conflict("owns_community") from exc
    except LastPlatformAdminError as exc:
        raise _conflict("last_platform_admin") from exc
    except UserNotFoundError as exc:
        raise _not_found() from exc
    await _audit(recorder, ops.USER_DELETE, admin=admin, target=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/users/{user_id}/platform-admin", status_code=status.HTTP_204_NO_CONTENT)
async def set_platform_admin(
    user_id: uuid.UUID,
    body: PlatformAdminRequest,
    admin: Annotated[User, Depends(require_platform_admin)],
    use_case: Annotated[SetPlatformAdmin, Depends(get_set_platform_admin)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> Response:
    try:
        await use_case(target_id=UserId(user_id), grant=body.grant)
    except LastPlatformAdminError as exc:
        raise _conflict("last_platform_admin") from exc
    except UserNotFoundError as exc:
        raise _not_found() from exc
    operation = (
        ops.USER_PLATFORM_ADMIN_GRANT if body.grant else ops.USER_PLATFORM_ADMIN_REVOKE
    )
    await _audit(recorder, operation, admin=admin, target=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _audit(
    recorder: AuditRecorder, operation: str, *, admin: User, target: uuid.UUID
) -> None:
    await recorder.record(
        AuditEvent(
            operation=operation,
            outcome=Outcome.SUCCESS,
            actor_id=admin.id.value,
            target_type=ops.TARGET_USER,
            target_id=target,
        )
    )


def _conflict(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail={"reason": reason}
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
