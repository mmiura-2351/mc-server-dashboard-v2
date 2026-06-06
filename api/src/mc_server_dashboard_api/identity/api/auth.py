"""Auth endpoints: login, token refresh, and logout (FR-AUTH-2, FR-AUTH-3).

Thin routers over the use cases resolved by the wiring layer. Login and refresh
return an access + refresh pair; both failure paths surface as a uniform 401 with
no detail that would distinguish the cause (SECURITY.md Section 2). Logout always
returns 204 (idempotent, no enumeration signal).

Refresh-token transport (issue #363): alongside the body-based contract that the
worker/CLI clients use, the refresh token also rides an httpOnly cookie for the
Web UI session (WEBUI_SPEC.md Section 7.1). Login *sets* the cookie; refresh and
logout *fall back* to it when the body carries no token, and re-set / clear it.
The body still carries the refresh token even for cookie clients, so the
body-based contract is byte-for-byte unchanged (non-breaking).

CSRF posture: the cookie is ``HttpOnly; Secure; SameSite=Strict; Path=/auth`` —
SameSite=Strict keeps the browser from attaching it to cross-site requests and
Path=/auth confines it to the auth endpoints. Refresh returns the rotated tokens
in the response body and performs no state change on behalf of an ambient
session, so it is not a useful CSRF target; the residual surface is a
logout-by-forced-request, whose only effect is to end the victim's own session.
A stricter posture (require a custom ``X-Requested-With`` header that cross-origin
callers cannot set without a CORS preflight) is deferred as an optional upgrade.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.config import Settings, TokenSettings
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_client_ip,
    get_login,
    get_logout,
    get_refresh_session,
    get_settings,
)
from mc_server_dashboard_api.http_problem import ProblemException, problem
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

# Fixed cookie attributes (the security posture, not operator knobs): confine the
# cookie to the auth endpoints and forbid cross-site attachment. See module docstring.
_COOKIE_PATH = "/auth"
_COOKIE_SAMESITE: Literal["strict"] = "strict"


def _set_refresh_cookie(
    response: Response, token: str, token_cfg: TokenSettings
) -> None:
    response.set_cookie(
        key=token_cfg.refresh_cookie_name,
        value=token,
        max_age=token_cfg.refresh_ttl_seconds,
        path=_COOKIE_PATH,
        httponly=True,
        secure=token_cfg.refresh_cookie_secure,
        samesite=_COOKIE_SAMESITE,
    )


def _clear_refresh_cookie(response: Response, token_cfg: TokenSettings) -> None:
    response.delete_cookie(
        key=token_cfg.refresh_cookie_name,
        path=_COOKIE_PATH,
        httponly=True,
        secure=token_cfg.refresh_cookie_secure,
        samesite=_COOKIE_SAMESITE,
    )


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    # Optional so cookie clients can POST an empty body; the cookie supplies the
    # token in that case (issue #363).
    refresh_token: str | None = Field(default=None, min_length=1)


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(default=None, min_length=1)


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
    response: Response,
    use_case: Annotated[Login, Depends(get_login)],
    client_ip: Annotated[str | None, Depends(get_client_ip)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    settings: Annotated[Settings, Depends(get_settings)],
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
    _set_refresh_cookie(response, result.pair.refresh_token, settings.auth.token)
    return TokenResponse.from_pair(result.pair)


@router.post("/auth/refresh")
async def refresh(
    body: RefreshRequest,
    request: Request,
    response: Response,
    use_case: Annotated[RefreshSession, Depends(get_refresh_session)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenResponse:
    token_cfg = settings.auth.token
    # Body wins; fall back to the cookie (its name is config-driven, so read it
    # off the raw request). With neither, return the uniform 401 without invoking
    # the use case (issue #363).
    refresh_token = body.refresh_token or request.cookies.get(
        token_cfg.refresh_cookie_name
    )
    if refresh_token is None:
        raise _unauthorized()
    # Reuse of an already-rotated token (RefreshTokenReuseError) triggers a family
    # revocation in the use case; record it as a DENIED security event attributed
    # to the affected user (FR-AUD-1). A plain unknown/expired token raises the
    # base InvalidRefreshTokenError and is not audited (proportionate: it is not a
    # token-theft signal). Both map to the same uniform 401 (no client signal).
    try:
        pair = await use_case(refresh_token=refresh_token)
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
    _set_refresh_cookie(response, pair.refresh_token, token_cfg)
    return TokenResponse.from_pair(pair)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: LogoutRequest,
    request: Request,
    use_case: Annotated[Logout, Depends(get_logout)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    token_cfg = settings.auth.token
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(response, token_cfg)
    # Body wins; fall back to the cookie. Logout is idempotent: with no token
    # there is nothing to revoke, but the cookie is still cleared and a clean 204
    # returned (no enumeration signal).
    refresh_token = body.refresh_token or request.cookies.get(
        token_cfg.refresh_cookie_name
    )
    if refresh_token is None:
        return response
    await use_case(refresh_token=refresh_token)
    await recorder.record(
        AuditEvent(operation=ops.AUTH_LOGOUT, outcome=Outcome.SUCCESS)
    )
    return response


def _unauthorized() -> ProblemException:
    return problem(
        status.HTTP_401_UNAUTHORIZED,
        "invalid_credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
