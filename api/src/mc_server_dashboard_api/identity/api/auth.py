"""Auth endpoints: login, token refresh, and logout (FR-AUTH-2, FR-AUTH-3).

Thin routers over the use cases resolved by the wiring layer. Login and refresh
return an access + refresh pair; both failure paths surface as a uniform 401 with
no detail that would distinguish the cause (SECURITY.md Section 2). Logout always
returns 204 (idempotent, no enumeration signal).
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
    get_client_ip,
    get_login,
    get_logout,
    get_refresh_session,
)
from mc_server_dashboard_api.identity.application.login import Login
from mc_server_dashboard_api.identity.application.logout import Logout
from mc_server_dashboard_api.identity.application.refresh_session import RefreshSession
from mc_server_dashboard_api.identity.application.token_pair import TokenPair
from mc_server_dashboard_api.identity.domain.errors import (
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    RefreshTokenReuseError,
)

router = APIRouter()


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class TokenResponse(BaseModel):
    """An issued access + refresh pair. ``token_type`` is the OAuth2 bearer hint."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"

    @classmethod
    def from_pair(cls, pair: TokenPair) -> "TokenResponse":
        return cls(access_token=pair.access_token, refresh_token=pair.refresh_token)


@router.post("/auth/login")
async def login(
    body: LoginRequest,
    use_case: Annotated[Login, Depends(get_login)],
    client_ip: Annotated[str | None, Depends(get_client_ip)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> TokenResponse:
    # On SUCCESS the row is actor-attributed (FR-AUD-1); on FAILURE actor_id
    # stays None (enumeration defence, SECURITY.md Section 2): the username/IP
    # forensic record lives in the login_attempt table.
    try:
        result = await use_case(
            username=body.username, password=body.password, ip=client_ip
        )
    except InvalidCredentialsError as exc:
        await recorder.record(
            AuditEvent(operation=ops.AUTH_LOGIN, outcome=Outcome.DENIED)
        )
        raise _unauthorized() from exc
    await recorder.record(
        AuditEvent(
            operation=ops.AUTH_LOGIN,
            outcome=Outcome.SUCCESS,
            actor_id=result.user_id,
        )
    )
    return TokenResponse.from_pair(result.pair)


@router.post("/auth/refresh")
async def refresh(
    body: RefreshRequest,
    use_case: Annotated[RefreshSession, Depends(get_refresh_session)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> TokenResponse:
    # Reuse of an already-rotated token (RefreshTokenReuseError) triggers a family
    # revocation in the use case; record it as a DENIED security event attributed
    # to the affected user (FR-AUD-1). A plain unknown/expired token raises the
    # base InvalidRefreshTokenError and is not audited (proportionate: it is not a
    # token-theft signal). Both map to the same uniform 401 (no client signal).
    try:
        pair = await use_case(refresh_token=body.refresh_token)
    except RefreshTokenReuseError as exc:
        await recorder.record(
            AuditEvent(
                operation=ops.AUTH_REFRESH_REUSE,
                outcome=Outcome.DENIED,
                actor_id=exc.user_id,
                target_type=ops.TARGET_USER,
                target_id=exc.user_id,
            )
        )
        raise _unauthorized() from exc
    except InvalidRefreshTokenError as exc:
        raise _unauthorized() from exc
    await recorder.record(
        AuditEvent(operation=ops.AUTH_REFRESH, outcome=Outcome.SUCCESS)
    )
    return TokenResponse.from_pair(pair)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: LogoutRequest,
    use_case: Annotated[Logout, Depends(get_logout)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> Response:
    await use_case(refresh_token=body.refresh_token)
    await recorder.record(
        AuditEvent(operation=ops.AUTH_LOGOUT, outcome=Outcome.SUCCESS)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid_credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
