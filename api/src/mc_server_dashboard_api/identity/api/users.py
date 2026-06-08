"""User account endpoints: registration (FR-AUTH-1) and self-service on /users/me.

The routers are thin: each resolves its use case via dependency injection (bound
in the wiring layer), runs it, and serialises the result. Domain errors are
translated to HTTP status codes here — a policy violation is 422 (carrying which
rule failed, never the password), a duplicate is 409, a wrong current password is
the same uniform 401 as login (no confirmation oracle), and a self-delete refused
by an ownership / last-admin invariant is 409 with a reason.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Response, status
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
    get_list_sessions,
    get_register_user,
    get_revoke_other_sessions,
    get_revoke_session,
    get_update_profile,
)
from mc_server_dashboard_api.http_datetime import UtcDatetime
from mc_server_dashboard_api.http_problem import ProblemException, problem
from mc_server_dashboard_api.identity.application.change_password import ChangePassword
from mc_server_dashboard_api.identity.application.delete_account import DeleteAccount
from mc_server_dashboard_api.identity.application.list_sessions import ListSessions
from mc_server_dashboard_api.identity.application.register_user import RegisterUser
from mc_server_dashboard_api.identity.application.revoke_other_sessions import (
    RevokeOtherSessions,
)
from mc_server_dashboard_api.identity.application.revoke_session import RevokeSession
from mc_server_dashboard_api.identity.application.update_profile import UpdateProfile
from mc_server_dashboard_api.identity.domain.entities import RefreshToken, User
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
from mc_server_dashboard_api.identity.domain.value_objects import (
    RefreshTokenId,
    UserId,
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
        raise problem(status.HTTP_403_FORBIDDEN, "registration_disabled") from exc
    except RegistrationThrottledError as exc:
        raise problem(
            status.HTTP_429_TOO_MANY_REQUESTS, "registration_throttled"
        ) from exc
    except (UsernameAlreadyExistsError, EmailAlreadyExistsError) as exc:
        raise problem(status.HTTP_409_CONFLICT, _conflict_reason(exc)) from exc
    except PasswordPolicyError as exc:
        raise problem(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.reason) from exc
    except (InvalidUsernameError, InvalidEmailError) as exc:
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT, _field_reason(exc)
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


class DeleteAccountRequest(BaseModel):
    # Re-authentication for this destructive self-service action (issue #420):
    # the caller's current password, verified before deletion. Mirrors
    # ChangePasswordRequest; the same min_length=1 makes a blank password a 422.
    password: str = Field(min_length=1)


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
        raise problem(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.reason) from exc
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
        raise problem(status.HTTP_409_CONFLICT, _conflict_reason(exc)) from exc
    except UserNotFoundError as exc:
        raise _user_gone() from exc
    except (InvalidUsernameError, InvalidEmailError) as exc:
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT, _field_reason(exc)
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
    body: DeleteAccountRequest,
    user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[DeleteAccount, Depends(get_delete_account)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> Response:
    # This destructive self-service action re-authenticates the caller (issue
    # #420): a wrong password is the same uniform 401 change_password returns, so
    # the endpoint is no password-confirmation oracle (SECURITY.md Section 2).
    # Capture the id before deletion so the fire-after-commit audit row still
    # attributes the actor/target once the user row is gone (the audit_log
    # actor_id is a soft reference with no FK, so it survives, DATABASE.md 9).
    deleted_id: UserId = user.id
    try:
        await use_case(user_id=deleted_id, password=body.password)
    except InvalidCredentialsError as exc:
        raise _unauthorized() from exc
    except CommunityOwnedError as exc:
        raise problem(status.HTTP_409_CONFLICT, "owns_community") from exc
    except LastPlatformAdminError as exc:
        raise problem(status.HTTP_409_CONFLICT, "last_platform_admin") from exc
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


class SessionResponse(BaseModel):
    """Safe metadata for one active refresh-token session (issue #387).

    Addressed by the row ``id`` (an opaque session id); the token hash/secret is
    deliberately never exposed. No client-hint field is included because none is
    stored on the row.
    """

    id: str
    created_at: UtcDatetime
    expires_at: UtcDatetime

    @classmethod
    def from_entity(cls, token: RefreshToken) -> "SessionResponse":
        return cls(
            id=str(token.id.value),
            created_at=token.issued_at,
            expires_at=token.expires_at,
        )


class RevokeOtherSessionsRequest(BaseModel):
    # The caller's current refresh token, so its session is spared (everywhere-
    # else logout). Optional: an access-token-only caller cannot present it, in
    # which case all active sessions are revoked (see RevokeOtherSessions).
    refresh_token: str | None = Field(default=None, min_length=1)


@router.get("/users/me/sessions")
async def list_sessions(
    user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[ListSessions, Depends(get_list_sessions)],
) -> list[SessionResponse]:
    """List the caller's active (non-revoked, non-expired) sessions (issue #387)."""

    tokens = await use_case(user_id=user.id)
    return [SessionResponse.from_entity(token) for token in tokens]


@router.delete(
    "/users/me/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_session(
    session_id: str,
    user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[RevokeSession, Depends(get_revoke_session)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> Response:
    # A malformed id, an unknown id, and an id owned by another user all map to the
    # same 404: the use case scopes the revoke to the caller, so it never reveals
    # whether the session exists or whom it belongs to (issue #387).
    try:
        token_id = RefreshTokenId(uuid.UUID(session_id))
    except ValueError as exc:
        raise _session_not_found() from exc
    revoked = await use_case(user_id=user.id, session_id=token_id)
    if not revoked:
        raise _session_not_found()
    await recorder.record(
        AuditEvent(
            operation=ops.AUTH_SESSION_REVOKE,
            outcome=Outcome.SUCCESS,
            actor_id=user.id.value,
            target_type=ops.TARGET_USER,
            target_id=user.id.value,
        )
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/users/me/sessions", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_other_sessions(
    user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[RevokeOtherSessions, Depends(get_revoke_other_sessions)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    # Optional body: a browser caller (whose refresh cookie is confined to
    # /api/auth and not sent here) can DELETE with no body at all.
    body: Annotated[RevokeOtherSessionsRequest | None, Body()] = None,
) -> Response:
    # Everywhere-else logout: the session matching the presented refresh token is
    # kept alive, the rest revoked. With no token presented, every active session
    # is revoked (the safe option — never another user's, issue #387).
    current = body.refresh_token if body is not None else None
    await use_case(user_id=user.id, current_refresh_token=current)
    await recorder.record(
        AuditEvent(
            operation=ops.AUTH_SESSION_REVOKE,
            outcome=Outcome.SUCCESS,
            actor_id=user.id.value,
            target_type=ops.TARGET_USER,
            target_id=user.id.value,
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


def _unauthorized() -> ProblemException:
    # Mirrors login's uniform 401 so a wrong current password is indistinguishable
    # from any other credential failure (SECURITY.md Section 2).
    return problem(
        status.HTTP_401_UNAUTHORIZED,
        "invalid_credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _session_not_found() -> ProblemException:
    # An unknown id, an id owned by another user, and a malformed id are
    # indistinguishable: a single 404 so the endpoint leaks neither the session's
    # existence nor its owner (issue #387 — 404, never 403).
    return problem(status.HTTP_404_NOT_FOUND, "session_not_found")


def _user_gone() -> ProblemException:
    # The token authenticated but the user row is gone (a concurrent self-delete
    # raced between get_current_user and the use case's get_by_id). The token now
    # references a non-existent principal, so it is treated like an invalidated
    # token: the same 401 invalid_token get_current_user returns, not a 500.
    return problem(
        status.HTTP_401_UNAUTHORIZED,
        "invalid_token",
        headers={"WWW-Authenticate": "Bearer"},
    )
