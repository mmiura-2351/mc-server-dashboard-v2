"""Endpoint tests for /auth/* and the protected GET /users/me.

The use cases are overridden with fakes so no database or JWT lib is touched
(NFR-TEST-1). Verifies status codes, the token-pair response shape, the uniform
401 on bad credentials/tokens, and that /users/me is gated by the Bearer header.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Iterator

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mc_server_dashboard_api.dependencies import (
    get_authenticate_request,
    get_login,
    get_logout,
    get_refresh_session,
    get_restore_session,
)
from mc_server_dashboard_api.identity.adapters.password_hasher import (
    BcryptPasswordHasher,
    build_dummy_password_hash,
)
from mc_server_dashboard_api.identity.application.login import Login, LoginResult
from mc_server_dashboard_api.identity.application.restore_session import RestoreResult
from mc_server_dashboard_api.identity.application.token_pair import TokenPair
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import (
    InvalidAccessTokenError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
)
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    UserId,
    Username,
)
from tests.identity.fakes import (
    FakeClock,
    FakeLoginAttemptStore,
    FakeTokenService,
    FakeUnitOfWork,
    RecordingFailureDelay,
    make_brute_force_config,
    make_user,
)


class _Fake:
    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[dict[str, str]] = []

    async def __call__(self, **kwargs: str) -> object:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


def _provider(value: object) -> Callable[[], object]:
    # A zero-parameter provider: FastAPI must not treat a captured default as a
    # request field (which happens with ``lambda v=value: v``).
    def _provide() -> object:
        return value

    return _provide


def _set_cookie_header(resp: httpx2.Response, name: str) -> str:
    # The TestClient runs over plain HTTP, so a Secure cookie is never stored in
    # the jar; inspect the raw Set-Cookie header(s) instead.
    headers: list[str] = resp.headers.get_list("set-cookie")
    for header in headers:
        if header.startswith(f"{name}="):
            return header
    raise AssertionError(f"no Set-Cookie for {name!r} in {headers!r}")


def _assert_no_set_cookie(resp: httpx2.Response, name: str) -> None:
    headers: list[str] = resp.headers.get_list("set-cookie")
    assert not any(header.startswith(f"{name}=") for header in headers), (
        f"unexpected Set-Cookie for {name!r} in {headers!r}"
    )


_shared_app: FastAPI


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    global _shared_app
    _shared_app = shared_app


def _client(**overrides: object) -> Iterator[TestClient]:
    app = _shared_app
    app.dependency_overrides.clear()
    for dependency, value in overrides.items():
        app.dependency_overrides[_PROVIDERS[dependency]] = _provider(value)
    with TestClient(app) as client:
        yield client


_PROVIDERS = {
    "login": get_login,
    "refresh": get_refresh_session,
    "restore": get_restore_session,
    "logout": get_logout,
    "authenticate": get_authenticate_request,
}


def test_login_returns_access_token() -> None:
    fake = _Fake(
        result=LoginResult(
            pair=TokenPair(access_token="acc", refresh_token="ref"),
            user_id=uuid.uuid4(),
        )
    )
    client = next(_client(login=fake))
    resp = client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    assert resp.status_code == 200
    assert resp.json() == {
        "access_token": "acc",
        "token_type": "bearer",
    }


def test_login_sets_refresh_cookie_with_security_attributes() -> None:
    # The SPA holds the access token in memory; the refresh token additionally
    # rides an httpOnly cookie scoped to the auth endpoints (issue #363).
    fake = _Fake(
        result=LoginResult(
            pair=TokenPair(access_token="acc", refresh_token="ref"),
            user_id=uuid.uuid4(),
        )
    )
    client = next(_client(login=fake))
    resp = client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    cookie = _set_cookie_header(resp, "mcd_refresh")
    assert "mcd_refresh=ref" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    # Starlette renders the SameSite value lowercase.
    assert "SameSite=strict" in cookie
    assert "Path=/api/auth" in cookie
    assert "Max-Age=1209600" in cookie


def test_login_passes_resolved_client_ip_to_use_case() -> None:
    # With proxy trust off (default), the resolved IP is the immediate peer; the
    # endpoint must forward it to the use case for the per-IP counter.
    fake = _Fake(
        result=LoginResult(
            pair=TokenPair(access_token="acc", refresh_token="ref"),
            user_id=uuid.uuid4(),
        )
    )
    client = next(_client(login=fake))
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    assert fake.calls == [{"username": "alice", "password": "pw", "ip": "testclient"}]


def test_login_invalid_credentials_returns_401() -> None:
    fake = _Fake(error=InvalidCredentialsError())
    client = next(_client(login=fake))
    resp = client.post("/api/auth/login", json={"username": "alice", "password": "bad"})
    assert resp.status_code == 401
    # RFC 9457 problem+json end-to-end through the real app factory (issue #371):
    # the content type and type URI are part of the contract, and the 401 still
    # carries WWW-Authenticate.
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.headers["www-authenticate"] == "Bearer"
    # No detail that distinguishes unknown-user from wrong-password.
    assert resp.json()["reason"] == "invalid_credentials"
    assert resp.json()["type"] == "urn:mcsd:error:invalid_credentials"


def test_login_locked_returns_retry_after_header() -> None:
    # When the use case raises InvalidCredentialsError with a retry_after
    # value, the endpoint must emit a Retry-After header (issue #637).
    fake = _Fake(error=InvalidCredentialsError(retry_after=600))
    client = next(_client(login=fake))
    resp = client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    assert resp.status_code == 401
    assert resp.headers["retry-after"] == "600"
    # The uniform 401 posture is preserved.
    assert resp.json()["reason"] == "invalid_credentials"
    assert resp.headers["www-authenticate"] == "Bearer"


def test_login_plain_failure_has_no_retry_after_header() -> None:
    # A normal failure (no lockout/throttle) must not emit Retry-After.
    fake = _Fake(error=InvalidCredentialsError())
    client = next(_client(login=fake))
    resp = client.post("/api/auth/login", json={"username": "alice", "password": "bad"})
    assert resp.status_code == 401
    assert "retry-after" not in resp.headers


def test_login_over_72_byte_password_under_bcrypt_returns_uniform_401() -> None:
    # Regression: a >72-byte login password under a bcrypt-configured hasher must
    # not 500 (the bcrypt adapter's verify() returns False instead of raising),
    # preserving the uniform-401 posture. Wires the real Login + real
    # BcryptPasswordHasher so the actual verify() path runs end to end.
    hasher = BcryptPasswordHasher()
    now = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
    user = User(
        id=UserId.new(),
        username=Username("alice"),
        email=EmailAddress("alice@example.com"),
        password_hash=build_dummy_password_hash("bcrypt", "Wm7!qz#Lp2vT"),
        created_at=now,
        updated_at=now,
    )
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    login = Login(
        uow=uow,
        attempts=FakeLoginAttemptStore(),
        brute_force=make_brute_force_config(),
        hasher=hasher,
        dummy_password_hash=build_dummy_password_hash("bcrypt", "dummy"),
        tokens=FakeTokenService(),
        clock=FakeClock(now),
        failure_delay=RecordingFailureDelay(),
        refresh_ttl=dt.timedelta(days=14),
    )
    client = next(_client(login=login))

    resp = client.post(
        "/api/auth/login", json={"username": "alice", "password": "A1!" + "x" * 100}
    )

    assert resp.status_code == 401
    assert resp.json()["reason"] == "invalid_credentials"


def test_refresh_returns_new_pair() -> None:
    fake = _Fake(result=TokenPair(access_token="acc2", refresh_token="ref2"))
    client = next(_client(refresh=fake))
    resp = client.post("/api/auth/refresh", json={"refresh_token": "ref1"})
    assert resp.status_code == 200
    assert resp.json()["access_token"] == "acc2"


def test_refresh_reads_token_from_cookie_when_body_omits_it() -> None:
    # Cookie clients POST an empty body; the refresh token is read from the
    # cookie and the rotated token re-set on the cookie (issue #363).
    fake = _Fake(result=TokenPair(access_token="acc2", refresh_token="ref2"))
    client = next(_client(refresh=fake))
    client.cookies.set("mcd_refresh", "ref1")
    resp = client.post("/api/auth/refresh", json={})
    assert resp.status_code == 200
    assert fake.calls == [{"refresh_token": "ref1", "superseded_token": None}]
    cookie = _set_cookie_header(resp, "mcd_refresh")
    assert "mcd_refresh=ref2" in cookie
    assert "HttpOnly" in cookie


def test_refresh_prefers_body_token_over_cookie() -> None:
    # The body-based contract wins when both are present (worker/CLI parity), and
    # the superseded cookie token is forwarded for revocation (issue #384).
    fake = _Fake(result=TokenPair(access_token="acc2", refresh_token="ref2"))
    client = next(_client(refresh=fake))
    client.cookies.set("mcd_refresh", "cookie-token")
    resp = client.post("/api/auth/refresh", json={"refresh_token": "body-token"})
    assert resp.status_code == 200
    assert fake.calls == [
        {"refresh_token": "body-token", "superseded_token": "cookie-token"}
    ]


def test_refresh_body_only_passes_no_superseded_token() -> None:
    # Single transport (body only): nothing to supersede, so the use case is called
    # without a superseded token (regression guard, issue #384).
    fake = _Fake(result=TokenPair(access_token="acc2", refresh_token="ref2"))
    client = next(_client(refresh=fake))
    resp = client.post("/api/auth/refresh", json={"refresh_token": "body-token"})
    assert resp.status_code == 200
    assert fake.calls == [{"refresh_token": "body-token", "superseded_token": None}]


def test_refresh_cookie_only_passes_no_superseded_token() -> None:
    # Single transport (cookie only): the cookie is the used token, not a
    # superseded one (regression guard, issue #384).
    fake = _Fake(result=TokenPair(access_token="acc2", refresh_token="ref2"))
    client = next(_client(refresh=fake))
    client.cookies.set("mcd_refresh", "cookie-token")
    resp = client.post("/api/auth/refresh", json={})
    assert resp.status_code == 200
    assert fake.calls == [{"refresh_token": "cookie-token", "superseded_token": None}]


def test_refresh_body_only_emits_no_set_cookie() -> None:
    # Body-only clients (worker/CLI) carried no cookie, so the rotated token must
    # not ride a Set-Cookie they never asked for (issue #372).
    fake = _Fake(result=TokenPair(access_token="acc2", refresh_token="ref2"))
    client = next(_client(refresh=fake))
    resp = client.post("/api/auth/refresh", json={"refresh_token": "ref1"})
    assert resp.status_code == 200
    _assert_no_set_cookie(resp, "mcd_refresh")


def test_refresh_with_cookie_rotates_cookie_even_when_body_token_wins() -> None:
    # Both transports present: the body token is used (precedence unchanged), but
    # because the request carried the cookie, the rotated token is re-set on it so
    # a browser session does not go stale (issue #372).
    fake = _Fake(result=TokenPair(access_token="acc2", refresh_token="ref2"))
    client = next(_client(refresh=fake))
    client.cookies.set("mcd_refresh", "cookie-token")
    resp = client.post("/api/auth/refresh", json={"refresh_token": "body-token"})
    assert resp.status_code == 200
    assert fake.calls == [
        {"refresh_token": "body-token", "superseded_token": "cookie-token"}
    ]
    cookie = _set_cookie_header(resp, "mcd_refresh")
    assert "mcd_refresh=ref2" in cookie


def test_refresh_without_body_or_cookie_returns_401() -> None:
    # No transport carries a token: uniform 401, use case not invoked.
    fake = _Fake(result=TokenPair(access_token="x", refresh_token="y"))
    client = next(_client(refresh=fake))
    resp = client.post("/api/auth/refresh", json={})
    assert resp.status_code == 401
    assert fake.calls == []


def test_refresh_invalid_token_returns_401() -> None:
    fake = _Fake(error=InvalidRefreshTokenError())
    client = next(_client(refresh=fake))
    resp = client.post("/api/auth/refresh", json={"refresh_token": "stale"})
    assert resp.status_code == 401


def test_session_returns_access_token_only() -> None:
    # The non-rotating bootstrap path (issue #512): a valid refresh cookie is
    # exchanged for an access token. No refresh_token in the body, no rotation.
    fake = _Fake(result=RestoreResult(access_token="acc3", user_id=uuid.uuid4()))
    client = next(_client(restore=fake))
    client.cookies.set("mcd_refresh", "live-cookie")
    resp = client.post("/api/auth/session")
    assert resp.status_code == 200
    assert resp.json() == {"access_token": "acc3", "token_type": "bearer"}
    assert fake.calls == [{"refresh_token": "live-cookie"}]


def test_session_emits_no_set_cookie() -> None:
    # Restore never rotates, so it must never re-set the refresh cookie — that is
    # the whole point: a page load can no longer leave a torn rotation in the jar.
    fake = _Fake(result=RestoreResult(access_token="acc3", user_id=uuid.uuid4()))
    client = next(_client(restore=fake))
    client.cookies.set("mcd_refresh", "live-cookie")
    resp = client.post("/api/auth/session")
    assert resp.status_code == 200
    _assert_no_set_cookie(resp, "mcd_refresh")


def test_session_without_cookie_returns_401() -> None:
    # No cookie carried: uniform 401, use case not invoked.
    fake = _Fake(result="acc3")
    client = next(_client(restore=fake))
    resp = client.post("/api/auth/session")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json()["reason"] == "invalid_credentials"
    assert fake.calls == []


def test_session_invalid_token_returns_uniform_401() -> None:
    # An unknown / expired / revoked cookie is rejected with the same uniform 401
    # as every other auth failure (no signal that distinguishes the cause).
    fake = _Fake(error=InvalidRefreshTokenError())
    client = next(_client(restore=fake))
    client.cookies.set("mcd_refresh", "stale-cookie")
    resp = client.post("/api/auth/session")
    assert resp.status_code == 401
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json()["reason"] == "invalid_credentials"


def test_logout_returns_204() -> None:
    fake = _Fake(result=None)
    client = next(_client(logout=fake))
    resp = client.post("/api/auth/logout", json={"refresh_token": "ref"})
    assert resp.status_code == 204
    assert fake.calls == [{"refresh_token": "ref", "superseded_token": None}]


def test_logout_reads_token_from_cookie_and_clears_it() -> None:
    fake = _Fake(result=None)
    client = next(_client(logout=fake))
    client.cookies.set("mcd_refresh", "ref")
    resp = client.post("/api/auth/logout", json={})
    assert resp.status_code == 204
    assert fake.calls == [{"refresh_token": "ref", "superseded_token": None}]
    cookie = _set_cookie_header(resp, "mcd_refresh")
    # Cleared: empty value plus an immediate expiry.
    assert 'mcd_refresh=""' in cookie or "mcd_refresh=;" in cookie
    assert "Path=/api/auth" in cookie


def test_logout_both_transports_forwards_superseded_cookie_token() -> None:
    # Both transports present: the body token is revoked and the superseded cookie
    # token is forwarded for revocation too (issue #384).
    fake = _Fake(result=None)
    client = next(_client(logout=fake))
    client.cookies.set("mcd_refresh", "cookie-token")
    resp = client.post("/api/auth/logout", json={"refresh_token": "body-token"})
    assert resp.status_code == 204
    assert fake.calls == [
        {"refresh_token": "body-token", "superseded_token": "cookie-token"}
    ]


def test_logout_body_only_emits_no_clearing_set_cookie() -> None:
    # Body-only clients (worker/CLI) carried no cookie, so logout must not emit a
    # clearing Set-Cookie they never asked for (issue #372).
    fake = _Fake(result=None)
    client = next(_client(logout=fake))
    resp = client.post("/api/auth/logout", json={"refresh_token": "ref"})
    assert resp.status_code == 204
    assert fake.calls == [{"refresh_token": "ref", "superseded_token": None}]
    _assert_no_set_cookie(resp, "mcd_refresh")


def test_logout_without_body_or_cookie_returns_204() -> None:
    # Idempotent: nothing to revoke, still a clean 204, use case not invoked.
    fake = _Fake(result=None)
    client = next(_client(logout=fake))
    resp = client.post("/api/auth/logout", json={})
    assert resp.status_code == 204
    assert fake.calls == []
    # No cookie was carried, so none is cleared (issue #372).
    _assert_no_set_cookie(resp, "mcd_refresh")


def test_me_returns_user_with_valid_bearer() -> None:
    user = make_user()
    fake = _Fake(result=user)
    client = next(_client(authenticate=fake))
    resp = client.get("/api/users/me", headers={"Authorization": "Bearer good-token"})
    assert resp.status_code == 200
    assert resp.json()["username"] == "alice"
    assert fake.calls == [{"access_token": "good-token"}]


def test_me_without_bearer_returns_401() -> None:
    fake = _Fake(result=make_user())
    client = next(_client(authenticate=fake))
    resp = client.get("/api/users/me")
    assert resp.status_code == 401


def test_me_with_invalid_token_returns_401() -> None:
    fake = _Fake(error=InvalidAccessTokenError())
    client = next(_client(authenticate=fake))
    resp = client.get("/api/users/me", headers={"Authorization": "Bearer bad"})
    assert resp.status_code == 401
    assert resp.json()["reason"] == "invalid_token"
