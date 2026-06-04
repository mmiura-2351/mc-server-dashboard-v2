"""POST /users: register a new global user account (FR-AUTH-1).

The router is thin: it resolves the :class:`RegisterUser` use case via dependency
injection (bound in the wiring layer), runs it, and serialises the created user.
Domain errors are translated to HTTP status codes here — a policy violation is
422 (carrying which rule failed, never the password), a duplicate is 409.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_current_user,
    get_register_user,
)
from mc_server_dashboard_api.identity.application.register_user import RegisterUser
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import (
    EmailAlreadyExistsError,
    InvalidEmailError,
    InvalidUsernameError,
    PasswordPolicyError,
    UsernameAlreadyExistsError,
)

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
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> UserResponse:
    try:
        user = await use_case(
            username=body.username, email=body.email, password=body.password
        )
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
