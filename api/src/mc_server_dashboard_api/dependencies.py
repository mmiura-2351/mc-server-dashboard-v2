"""Composition root: the single place adapters are bound to Ports.

This is the edge wiring (ARCHITECTURE.md Section 2.1). It is the only module
allowed to import ``adapters`` alongside ``application``/``domain`` and to read
configuration. Routers depend on the Port-returning provider functions here via
FastAPI's ``Depends``; tests override the providers to inject fakes.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncEngine

from mc_server_dashboard_api.config import (
    BruteForceSettings,
    PasswordSettings,
    Settings,
    TokenSettings,
)
from mc_server_dashboard_api.core.adapters.database import (
    SqlAlchemyDatabasePing,
    create_session_factory,
)
from mc_server_dashboard_api.core.domain.health import DatabasePing
from mc_server_dashboard_api.identity.adapters.client_ip import (
    forwarded_for_header,
    resolve_client_ip,
)
from mc_server_dashboard_api.identity.adapters.clock import SystemClock
from mc_server_dashboard_api.identity.adapters.common_passwords import (
    load_common_passwords,
)
from mc_server_dashboard_api.identity.adapters.login_attempt_store import (
    SqlAlchemyLoginAttemptStore,
)
from mc_server_dashboard_api.identity.adapters.login_failure_delay import (
    FixedLoginFailureDelay,
)
from mc_server_dashboard_api.identity.adapters.password_hasher import (
    Argon2PasswordHasher,
    BcryptPasswordHasher,
)
from mc_server_dashboard_api.identity.adapters.sleeper import AsyncioSleeper
from mc_server_dashboard_api.identity.adapters.token_service import JwtTokenService
from mc_server_dashboard_api.identity.adapters.unit_of_work import SqlAlchemyUnitOfWork
from mc_server_dashboard_api.identity.application.authenticate_request import (
    AuthenticateRequest,
)
from mc_server_dashboard_api.identity.application.login import Login
from mc_server_dashboard_api.identity.application.logout import Logout
from mc_server_dashboard_api.identity.application.refresh_session import RefreshSession
from mc_server_dashboard_api.identity.application.register_user import RegisterUser
from mc_server_dashboard_api.identity.domain.brute_force import BruteForceConfig
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import InvalidAccessTokenError
from mc_server_dashboard_api.identity.domain.password_hasher import PasswordHasher
from mc_server_dashboard_api.identity.domain.password_policy import PasswordPolicy
from mc_server_dashboard_api.identity.domain.token_service import TokenService


def get_engine(request: Request) -> AsyncEngine:
    """Return the async engine the app factory stored on application state."""

    engine: AsyncEngine = request.app.state.engine
    return engine


def get_settings(request: Request) -> Settings:
    """Return the resolved settings the app factory stored on application state."""

    settings: Settings = request.app.state.settings
    return settings


def get_database_ping(request: Request) -> DatabasePing:
    """Bind the :class:`DatabasePing` Port to its SQLAlchemy adapter."""

    return SqlAlchemyDatabasePing(get_engine(request))


def _build_password_hasher(password: PasswordSettings) -> PasswordHasher:
    """Construct the PasswordHasher adapter named by ``auth.password.hash``."""

    if password.hash == "bcrypt":
        return BcryptPasswordHasher()
    return Argon2PasswordHasher()


# A throwaway value to derive the dummy verification hash from. Verifying a real
# password against this hash always fails; its only purpose is to give the
# unknown-user login path the same cost as a wrong-password verify.
_DUMMY_VERIFY_PLAINTEXT = "dummy-password-for-timing-equalization"


@lru_cache(maxsize=2)
def _dummy_password_hash(algorithm: str) -> str:
    """Pre-compute the static dummy hash for ``algorithm`` once (login timing)."""

    hasher = BcryptPasswordHasher() if algorithm == "bcrypt" else Argon2PasswordHasher()
    return hasher.hash(_DUMMY_VERIFY_PLAINTEXT)


@lru_cache(maxsize=1)
def _common_passwords() -> frozenset[str]:
    """Load the common-password blocklist once and reuse it across requests."""

    return load_common_passwords()


def _build_password_policy(password: PasswordSettings) -> PasswordPolicy:
    """Build the pure :class:`PasswordPolicy` from the configured knobs."""

    common = _common_passwords() if password.check_common_list else frozenset()
    return PasswordPolicy(
        min_length=password.min_length,
        max_length=password.max_length,
        require_complexity=password.require_complexity,
        check_common_list=password.check_common_list,
        forbid_user_info=password.forbid_user_info,
        forbid_simple_patterns=password.forbid_simple_patterns,
        common_passwords=common,
    )


def get_register_user(request: Request) -> RegisterUser:
    """Assemble the :class:`RegisterUser` use case from config-selected adapters."""

    settings = get_settings(request)
    session_factory = create_session_factory(get_engine(request))
    return RegisterUser(
        uow=SqlAlchemyUnitOfWork(session_factory),
        hasher=_build_password_hasher(settings.auth.password),
        clock=SystemClock(),
        policy=_build_password_policy(settings.auth.password),
    )


def _build_token_service(token: TokenSettings, clock: SystemClock) -> TokenService:
    """Construct the JWT TokenService adapter from ``auth.token.*``.

    The signing key is required to mount the auth endpoints; the app factory
    enforces that at startup, so it is non-None here.
    """

    assert token.signing_key is not None
    return JwtTokenService(
        signing_key=token.signing_key,
        algorithm=token.algorithm,
        access_ttl=dt.timedelta(seconds=token.access_ttl_seconds),
        clock=clock,
    )


def _build_brute_force_config(brute_force: BruteForceSettings) -> BruteForceConfig:
    """Map the ``auth.brute_force.*`` knobs to the domain config value."""

    return BruteForceConfig(
        enabled=brute_force.enabled,
        username_threshold=brute_force.username_threshold,
        username_window=dt.timedelta(seconds=brute_force.username_window_seconds),
        ip_threshold=brute_force.ip_threshold,
        ip_window=dt.timedelta(seconds=brute_force.ip_window_seconds),
        lockout_base=dt.timedelta(seconds=brute_force.lockout_base_seconds),
        lockout_max=dt.timedelta(seconds=brute_force.lockout_max_seconds),
        delay=dt.timedelta(milliseconds=brute_force.delay_ms),
    )


def get_login(request: Request) -> Login:
    """Assemble the :class:`Login` use case from config-selected adapters."""

    settings = get_settings(request)
    clock = SystemClock()
    session_factory = create_session_factory(get_engine(request))
    brute_force = _build_brute_force_config(settings.auth.brute_force)
    return Login(
        uow=SqlAlchemyUnitOfWork(session_factory),
        attempts=SqlAlchemyLoginAttemptStore(session_factory),
        brute_force=brute_force,
        hasher=_build_password_hasher(settings.auth.password),
        dummy_password_hash=_dummy_password_hash(settings.auth.password.hash),
        tokens=_build_token_service(settings.auth.token, clock),
        clock=clock,
        failure_delay=FixedLoginFailureDelay(
            delay=brute_force.delay, sleeper=AsyncioSleeper()
        ),
        refresh_ttl=dt.timedelta(seconds=settings.auth.token.refresh_ttl_seconds),
    )


def get_client_ip(request: Request) -> str | None:
    """Resolve the trustworthy client IP for the request (SECURITY.md Section 4).

    Honors the forwarded-for header only from configured trusted peers; otherwise
    the immediate socket peer. Feeds the per-IP brute-force counter.
    """

    proxy = get_settings(request).auth.proxy
    peer_ip = request.client.host if request.client is not None else None
    return resolve_client_ip(
        peer_ip=peer_ip,
        forwarded_for=forwarded_for_header(request.headers),
        trust_forwarded_headers=proxy.trust_forwarded_headers,
        trusted_proxies=proxy.trusted_proxies,
    )


def get_refresh_session(request: Request) -> RefreshSession:
    """Assemble the :class:`RefreshSession` use case from config-selected adapters."""

    settings = get_settings(request)
    clock = SystemClock()
    session_factory = create_session_factory(get_engine(request))
    return RefreshSession(
        uow=SqlAlchemyUnitOfWork(session_factory),
        tokens=_build_token_service(settings.auth.token, clock),
        clock=clock,
        refresh_ttl=dt.timedelta(seconds=settings.auth.token.refresh_ttl_seconds),
    )


def get_logout(request: Request) -> Logout:
    """Assemble the :class:`Logout` use case from config-selected adapters."""

    settings = get_settings(request)
    clock = SystemClock()
    session_factory = create_session_factory(get_engine(request))
    return Logout(
        uow=SqlAlchemyUnitOfWork(session_factory),
        tokens=_build_token_service(settings.auth.token, clock),
        clock=clock,
    )


def get_authenticate_request(request: Request) -> AuthenticateRequest:
    """Assemble the :class:`AuthenticateRequest` use case (current-user lookup)."""

    settings = get_settings(request)
    session_factory = create_session_factory(get_engine(request))
    return AuthenticateRequest(
        uow=SqlAlchemyUnitOfWork(session_factory),
        tokens=_build_token_service(settings.auth.token, SystemClock()),
    )


# Extracts the ``Authorization: Bearer <token>`` header; a missing/blank header
# yields 403 by default. ``auto_error=False`` lets us return a uniform 401.
_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    use_case: Annotated[AuthenticateRequest, Depends(get_authenticate_request)],
) -> User:
    """FastAPI dependency: the authenticated user behind a Bearer access token.

    Every protected endpoint depends on this. A missing, malformed, or expired
    token is a uniform 401 (no detail that aids enumeration).
    """

    if credentials is None:
        raise _unauthenticated()
    try:
        return await use_case(access_token=credentials.credentials)
    except InvalidAccessTokenError as exc:
        raise _unauthenticated() from exc


def _unauthenticated() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid_token",
        headers={"WWW-Authenticate": "Bearer"},
    )
