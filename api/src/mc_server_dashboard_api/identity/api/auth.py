"""Auth endpoints: login, token refresh, and logout (FR-AUTH-2, FR-AUTH-3).

Thin routers over the use cases resolved by the wiring layer. Login returns only
the access token in the body and delivers the refresh token solely via the
httpOnly cookie (issue #636); refresh returns the full access + refresh pair.
Both failure paths surface as a uniform 401 with no detail that would distinguish
the cause (SECURITY.md Section 2). Logout always returns 204 (idempotent, no
enumeration signal).

Refresh-token transport (issue #363): alongside the body-based contract that the
worker/CLI clients use, the refresh token also rides an httpOnly cookie for the
Web UI session (WEBUI_SPEC.md Section 7.1). Login *sets* the cookie and returns
only the access token in the body — the cookie is the sole refresh-token transport
from login (issue #636). Refresh and logout *fall back* to the cookie when the
body carries no token. They re-set / clear the cookie only when the request itself
carried it, so a body-only request leaves the response headers byte-for-byte
unchanged — no rotated or clearing Set-Cookie that a non-browser client never
asked for (issue #372).

CSRF posture: the cookie is ``HttpOnly; Secure; SameSite=Strict; Path=/api/auth``
— SameSite=Strict keeps the browser from attaching it to cross-site requests and
Path=/api/auth confines it to the auth endpoints. Refresh returns the rotated tokens
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
    get_restore_session,
    get_settings,
)
from mc_server_dashboard_api.http_problem import ProblemException, problem
from mc_server_dashboard_api.identity.application.login import Login
from mc_server_dashboard_api.identity.application.logout import Logout
from mc_server_dashboard_api.identity.application.refresh_session import RefreshSession
from mc_server_dashboard_api.identity.application.restore_session import RestoreSession
from mc_server_dashboard_api.identity.application.token_pair import TokenPair
from mc_server_dashboard_api.identity.domain.errors import (
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    RefreshTokenReuseError,
)

router = APIRouter()

# Fixed cookie attributes (the security posture, not operator knobs): confine the
# cookie to the auth endpoints and forbid cross-site attachment. See module docstring.
_COOKIE_PATH = "/api/auth"
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


class AccessTokenResponse(BaseModel):
    """Access-token-only response — used by login (#636) and session restore (#512).

    No ``refresh_token``: login delivers it via the httpOnly cookie; restore
    does not rotate, so there is no new refresh secret to hand back.
    """

    access_token: str
    token_type: str = "bearer"


@router.post("/auth/login")
async def login(
    body: LoginRequest,
    response: Response,
    use_case: Annotated[Login, Depends(get_login)],
    client_ip: Annotated[str | None, Depends(get_client_ip)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AccessTokenResponse:
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
    return AccessTokenResponse(access_token=result.pair.access_token)


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
    cookie_token = request.cookies.get(token_cfg.refresh_cookie_name)
    refresh_token = body.refresh_token or cookie_token
    if refresh_token is None:
        raise _unauthorized()
    # Both transports carried a token: the body wins, but the cookie's token is
    # superseded -- the browser jar will be overwritten with the body token's
    # successor below, so revoke the cookie token too rather than leave it dangling
    # valid server-side (issue #384). Single transport: nothing to supersede.
    superseded_token = cookie_token if body.refresh_token is not None else None
    # Reuse of an already-rotated token (RefreshTokenReuseError) triggers a family
    # revocation in the use case; record it as a DENIED security event attributed
    # to the affected user (FR-AUD-1). A plain unknown/expired token raises the
    # base InvalidRefreshTokenError and is not audited (proportionate: it is not a
    # token-theft signal). Both map to the same uniform 401 (no client signal).
    try:
        pair = await use_case(
            refresh_token=refresh_token, superseded_token=superseded_token
        )
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
    # Rotate the cookie only for clients that carried it, so the body-only
    # worker/CLI contract stays byte-identical including headers (issue #372).
    if cookie_token is not None:
        _set_refresh_cookie(response, pair.refresh_token, token_cfg)
    return TokenResponse.from_pair(pair)


@router.post("/auth/session")
async def session(
    request: Request,
    use_case: Annotated[RestoreSession, Depends(get_restore_session)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AccessTokenResponse:
    # The Web UI bootstrap: turn the httpOnly refresh cookie into a fresh access
    # token WITHOUT rotating (issue #512). No Set-Cookie, no body token — a page
    # load can no longer leave a torn rotation in the jar. Rotation and
    # reuse-detection stay on /auth/refresh (the periodic in-session path), so the
    # theft model is unchanged. This endpoint is cookie-only: the body-based
    # worker/CLI clients use /auth/refresh, which carries the refresh token they
    # need rotated. A missing or invalid cookie returns the uniform 401.
    cookie_token = request.cookies.get(settings.auth.token.refresh_cookie_name)
    if cookie_token is None:
        raise _unauthorized()
    try:
        result = await use_case(refresh_token=cookie_token)
    except InvalidRefreshTokenError as exc:
        raise _unauthorized() from exc
    # Restore no longer collides with a rotation, so it raised no theft signal
    # against an idle victim (issue #530). Record a SUCCESS row attributed to the
    # session's user so operators can see session-restore activity per family —
    # the explicit replacement for the incidental detection signal restore lacks.
    # Only successes are recorded: a missing/invalid cookie stays a silent 401
    # (no enumeration signal), matching the plain-bad-token posture on /refresh.
    await recorder.record(
        AuditEvent(
            operation=ops.AUTH_SESSION_RESTORE,
            outcome=Outcome.SUCCESS,
            actor_id=result.user_id,
            target_type=ops.TARGET_USER,
            target_id=result.user_id,
        )
    )
    return AccessTokenResponse(access_token=result.access_token)


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
    # Clear the cookie only for clients that carried it, so the body-only
    # worker/CLI contract stays byte-identical including headers (issue #372).
    cookie_token = request.cookies.get(token_cfg.refresh_cookie_name)
    if cookie_token is not None:
        _clear_refresh_cookie(response, token_cfg)
    # Body wins; fall back to the cookie. Logout is idempotent: with no token
    # there is nothing to revoke, a clean 204 is returned (no enumeration signal).
    refresh_token = body.refresh_token or cookie_token
    if refresh_token is None:
        return response
    # Both transports carried a token: revoke the superseded cookie token too, so
    # logout does not leave it dangling valid server-side (issue #384). Single
    # transport: nothing to supersede.
    superseded_token = cookie_token if body.refresh_token is not None else None
    await use_case(refresh_token=refresh_token, superseded_token=superseded_token)
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
