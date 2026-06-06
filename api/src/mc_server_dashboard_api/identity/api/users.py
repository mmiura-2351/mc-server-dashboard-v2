"""User account endpoints: registration (FR-AUTH-1) and self-service on /users/me.

The routers are thin: each resolves its use case via dependency injection (bound
in the wiring layer), runs it, and serialises the result. Domain errors are
translated to HTTP status codes here — a policy violation is 422 (carrying which
rule failed, never the password), a duplicate is 409, a wrong current password is
the same uniform 401 as login (no confirmation oracle), and a self-delete refused
by an ownership / last-admin invariant is 409 with a reason.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_change_password,
    get_client_ip,
    get_current_user,
    get_delete_account,
    get_register_user,
    get_update_profile,
)
from mc_server_dashboard_api.identity.application.change_password import ChangePassword
from mc_server_dashboard_api.identity.application.delete_account import DeleteAccount
from mc_server_dashboard_api.identity.application.register_user import RegisterUser
from mc_server_dashboard_api.identity.application.update_profile import UpdateProfile
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import (
    CommunityOwnedError,
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    InvalidEmailError,
    InvalidUsernameError,
    LastPlatformAdminError,
    PasswordPolicyError,
    RegistrationDisabledError,
    RegistrationThrottledError,
    UsernameAlreadyExistsError,
    UserNotFoundError,
)
from mc_server_dashboard_api.identity.domain.value_objects import UserId

router = APIRouter()


class RegisterUserRequest(BaseModel):
    username: str = Field(min_length=1)
    # Structural email validation lives in the EmailAddress value object; the
    # router maps its InvalidEmailError to 422. Avoids an extra validator dep.
    email: str = Field(min_length=1)
    # Cheap DoS guard so an oversized body is rejected before the password policy
    # runs; the policy enforces the real (configurable, far tighter) bounds.
    password: str = Field(min_length=1, max_length=1024)


class UserResponse(BaseModel):
    """Public view of a user; the password hash is deliberately never exposed."""

    id: str
    username: str
    email: str
    is_platform_admin: bool

    @classmethod
    def from_entity(cls, user: User) -> "UserResponse":
        return cls(
            id=str(user.id.value),
            username=user.username.value,
            email=user.email.value,
            is_platform_admin=user.is_platform_admin,
        )


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def register_user(
    body: RegisterUserRequest,
    use_case: Annotated[RegisterUser, Depends(get_register_user)],
    client_ip: Annotated[str | None, Depends(get_client_ip)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> UserResponse:
    # Open registration is unauthenticated, so it carries two abuse controls
    # (issue #362): a closed endpoint is 403; a per-IP flood is 429. The per-IP
    # cap reuses login's trusted-proxy client-IP resolution (``get_client_ip``).
    try:
        user = await use_case(
            username=body.username,
            email=body.email,
            password=body.password,
            ip=client_ip,
        )
    except RegistrationDisabledError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"reason": "registration_disabled"},
        ) from exc
    except RegistrationThrottledError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"reason": "registration_throttled"},
        ) from exc
    except (UsernameAlreadyExistsError, EmailAlreadyExistsError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": _conflict_reason(exc)},
        ) from exc
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": exc.reason},
        ) from exc
    except (InvalidUsernameError, InvalidEmailError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": _field_reason(exc)},
        ) from exc
    # Self-registration: the new user is both the actor and the target (FR-AUD-1).
    await recorder.record(
        AuditEvent(
            operation=ops.AUTH_REGISTER,
            outcome=Outcome.SUCCESS,
            actor_id=user.id.value,
            target_type=ops.TARGET_USER,
            target_id=user.id.value,
        )
    )
    return UserResponse.from_entity(user)


@router.get("/users/me")
async def read_current_user(
    user: Annotated[User, Depends(get_current_user)],
) -> UserResponse:
    """Return the authenticated user — the trivially protected endpoint."""

    return UserResponse.from_entity(user)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    # Cheap DoS guard so an oversized body is rejected before the policy runs; the
    # policy enforces the real (configurable) bounds, as on registration.
    new_password: str = Field(min_length=1, max_length=1024)


class UpdateProfileRequest(BaseModel):
    # Both optional: a self-service edit may change either field or both. Structural
    # validation lives in the value objects; the router maps their errors to 422.
    username: str | None = Field(default=None, min_length=1)
    email: str | None = Field(default=None, min_length=1)


@router.put("/users/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest,
    user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[ChangePassword, Depends(get_change_password)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> Response:
    # A wrong current password is the same uniform 401 login returns, so this
    # endpoint cannot be used as a password-confirmation oracle (SECURITY.md
    # Section 2). The in-flight access token may ride out its short TTL; every
    # refresh token is revoked by the use case so sessions cannot be renewed.
    try:
        await use_case(
            user_id=user.id,
            current_password=body.current_password,
            new_password=body.new_password,
        )
    except InvalidCredentialsError as exc:
        raise _unauthorized() from exc
    except UserNotFoundError as exc:
        raise _user_gone() from exc
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": exc.reason},
        ) from exc
    await recorder.record(
        AuditEvent(
            operation=ops.AUTH_PASSWORD_CHANGE,
            outcome=Outcome.SUCCESS,
            actor_id=user.id.value,
            target_type=ops.TARGET_USER,
            target_id=user.id.value,
        )
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/users/me")
async def update_profile(
    body: UpdateProfileRequest,
    user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[UpdateProfile, Depends(get_update_profile)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> UserResponse:
    # No token rotation is needed: the access token's only identity claim is the
    # user id (``sub``), which does not change here, so outstanding tokens remain
    # valid (JwtTokenService claims are id-based).
    try:
        updated = await use_case(
            user_id=user.id, username=body.username, email=body.email
        )
    except (UsernameAlreadyExistsError, EmailAlreadyExistsError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": _conflict_reason(exc)},
        ) from exc
    except UserNotFoundError as exc:
        raise _user_gone() from exc
    except (InvalidUsernameError, InvalidEmailError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": _field_reason(exc)},
        ) from exc
    await recorder.record(
        AuditEvent(
            operation=ops.AUTH_PROFILE_UPDATE,
            outcome=Outcome.SUCCESS,
            actor_id=user.id.value,
            target_type=ops.TARGET_USER,
            target_id=user.id.value,
        )
    )
    return UserResponse.from_entity(updated)


@router.delete("/users/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[DeleteAccount, Depends(get_delete_account)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> Response:
    # Capture the id before deletion so the fire-after-commit audit row still
    # attributes the actor/target once the user row is gone (the audit_log
    # actor_id is a soft reference with no FK, so it survives, DATABASE.md 9).
    deleted_id: UserId = user.id
    try:
        await use_case(user_id=deleted_id)
    except CommunityOwnedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "owns_community"},
        ) from exc
    except LastPlatformAdminError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "last_platform_admin"},
        ) from exc
    except UserNotFoundError as exc:
        raise _user_gone() from exc
    await recorder.record(
        AuditEvent(
            operation=ops.AUTH_ACCOUNT_DELETE,
            outcome=Outcome.SUCCESS,
            actor_id=deleted_id.value,
            target_type=ops.TARGET_USER,
            target_id=deleted_id.value,
        )
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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


def _unauthorized() -> HTTPException:
    # Mirrors login's uniform 401 so a wrong current password is indistinguishable
    # from any other credential failure (SECURITY.md Section 2).
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid_credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _user_gone() -> HTTPException:
    # The token authenticated but the user row is gone (a concurrent self-delete
    # raced between get_current_user and the use case's get_by_id). The token now
    # references a non-existent principal, so it is treated like an invalidated
    # token: the same 401 invalid_token get_current_user returns, not a 500.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid_token",
        headers={"WWW-Authenticate": "Bearer"},
    )
