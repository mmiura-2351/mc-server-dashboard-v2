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

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.dependencies import (
    get_admin_create_user,
    get_admin_delete_user,
    get_audit_recorder,
    get_list_users,
    get_set_platform_admin,
    get_set_user_active,
    require_platform_admin,
)
from mc_server_dashboard_api.http_datetime import UtcDatetime
from mc_server_dashboard_api.http_problem import ProblemException, problem
from mc_server_dashboard_api.identity.api.users import UserResponse
from mc_server_dashboard_api.identity.application.admin_create_user import (
    AdminCreateUser,
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
    EmailAlreadyExistsError,
    InvalidEmailError,
    InvalidUsernameError,
    LastPlatformAdminError,
    PasswordPolicyError,
    SelfTargetError,
    UsernameAlreadyExistsError,
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
    created_at: UtcDatetime

    @classmethod
    def from_entity(cls, user: User) -> "AdminUserResponse":
        return cls(
            id=str(user.id.value),
            username=user.username.value,
            email=user.email.value,
            is_platform_admin=user.is_platform_admin,
            active=user.active,
            created_at=user.created_at,
        )


class UserListResponse(BaseModel):
    users: list[AdminUserResponse]
    total: int
    limit: int
    offset: int


class PlatformAdminRequest(BaseModel):
    grant: bool


class AdminCreateUserRequest(BaseModel):
    # Same input contract as open registration (username, email, password); the
    # structural bounds mirror ``RegisterUserRequest`` so the admin path and the
    # open path validate identically before the password policy runs.
    username: str = Field(min_length=1)
    email: str = Field(min_length=1)
    password: str = Field(min_length=1, max_length=1024)


@router.post("/admin/users", status_code=status.HTTP_201_CREATED)
async def admin_create_user(
    body: AdminCreateUserRequest,
    admin: Annotated[User, Depends(require_platform_admin)],
    use_case: Annotated[AdminCreateUser, Depends(get_admin_create_user)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> UserResponse:
    # Admin-gated account provisioning (issue #368): exempt from the open-flag and
    # the per-IP cap that guard the unauthenticated ``POST /users``. Reuses the
    # registration error -> status mapping so validation and duplicates behave
    # identically on both paths.
    try:
        user = await use_case(
            username=body.username,
            email=body.email,
            password=body.password,
        )
    except (UsernameAlreadyExistsError, EmailAlreadyExistsError) as exc:
        raise problem(status.HTTP_409_CONFLICT, _conflict_reason(exc)) from exc
    except PasswordPolicyError as exc:
        raise problem(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.reason) from exc
    except (InvalidUsernameError, InvalidEmailError) as exc:
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT, _field_reason(exc)
        ) from exc
    await _audit(recorder, ops.USER_CREATE, admin=admin, target=user.id.value)
    # First-user bootstrap (#909): if this admin-created account is the very first
    # user on the database, persist_new_user auto-grants it platform admin. The
    # grant is shared with open registration, so audit it the same way that route
    # does -- unreachable on an empty DB via HTTP (admin-gated), but a future
    # caller of this shared path inherits an audited grant.
    if user.is_platform_admin:
        await _audit(
            recorder, ops.USER_PLATFORM_ADMIN_GRANT, admin=admin, target=user.id.value
        )
    return UserResponse.from_entity(user)


@router.get("/admin/users", dependencies=[Depends(require_platform_admin)])
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


@router.post(
    "/admin/users/{user_id}/deactivate", status_code=status.HTTP_204_NO_CONTENT
)
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


@router.post(
    "/admin/users/{user_id}/reactivate", status_code=status.HTTP_204_NO_CONTENT
)
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


@router.delete("/admin/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
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


@router.put(
    "/admin/users/{user_id}/platform-admin", status_code=status.HTTP_204_NO_CONTENT
)
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


def _conflict(reason: str) -> ProblemException:
    return problem(status.HTTP_409_CONFLICT, reason)


def _not_found() -> ProblemException:
    return problem(status.HTTP_404_NOT_FOUND, "not_found")


def _conflict_reason(exc: Exception) -> str:
    return (
        "username_taken"
        if isinstance(exc, UsernameAlreadyExistsError)
        else "email_taken"
    )


def _field_reason(exc: Exception) -> str:
    return (
        "invalid_username" if isinstance(exc, InvalidUsernameError) else "invalid_email"
    )
