"""Endpoint tests for /auth/* and the protected GET /users/me.

The use cases are overridden with fakes so no database or JWT lib is touched
(NFR-TEST-1). Verifies status codes, the token-pair response shape, the uniform
401 on bad credentials/tokens, and that /users/me is gated by the Bearer header.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.dependencies import (
    get_authenticate_request,
    get_login,
    get_logout,
    get_refresh_session,
)
from mc_server_dashboard_api.identity.application.token_pair import TokenPair
from mc_server_dashboard_api.identity.domain.errors import (
    InvalidAccessTokenError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
)
from tests.identity.fakes import make_user


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


def _client(**overrides: object) -> Iterator[TestClient]:
    app = create_app()
    for dependency, value in overrides.items():
        app.dependency_overrides[_PROVIDERS[dependency]] = _provider(value)
    with TestClient(app) as client:
        yield client


_PROVIDERS = {
    "login": get_login,
    "refresh": get_refresh_session,
    "logout": get_logout,
    "authenticate": get_authenticate_request,
}


def test_login_returns_token_pair() -> None:
    fake = _Fake(result=TokenPair(access_token="acc", refresh_token="ref"))
    client = next(_client(login=fake))
    resp = client.post("/auth/login", json={"username": "alice", "password": "pw"})
    assert resp.status_code == 200
    assert resp.json() == {
        "access_token": "acc",
        "refresh_token": "ref",
        "token_type": "bearer",
    }


def test_login_passes_resolved_client_ip_to_use_case() -> None:
    # With proxy trust off (default), the resolved IP is the immediate peer; the
    # endpoint must forward it to the use case for the per-IP counter.
    fake = _Fake(result=TokenPair(access_token="acc", refresh_token="ref"))
    client = next(_client(login=fake))
    client.post("/auth/login", json={"username": "alice", "password": "pw"})
    assert fake.calls == [{"username": "alice", "password": "pw", "ip": "testclient"}]


def test_login_invalid_credentials_returns_401() -> None:
    fake = _Fake(error=InvalidCredentialsError())
    client = next(_client(login=fake))
    resp = client.post("/auth/login", json={"username": "alice", "password": "bad"})
    assert resp.status_code == 401
    # No detail that distinguishes unknown-user from wrong-password.
    assert resp.json()["detail"] == "invalid_credentials"


def test_refresh_returns_new_pair() -> None:
    fake = _Fake(result=TokenPair(access_token="acc2", refresh_token="ref2"))
    client = next(_client(refresh=fake))
    resp = client.post("/auth/refresh", json={"refresh_token": "ref1"})
    assert resp.status_code == 200
    assert resp.json()["access_token"] == "acc2"


def test_refresh_invalid_token_returns_401() -> None:
    fake = _Fake(error=InvalidRefreshTokenError())
    client = next(_client(refresh=fake))
    resp = client.post("/auth/refresh", json={"refresh_token": "stale"})
    assert resp.status_code == 401


def test_logout_returns_204() -> None:
    fake = _Fake(result=None)
    client = next(_client(logout=fake))
    resp = client.post("/auth/logout", json={"refresh_token": "ref"})
    assert resp.status_code == 204
    assert fake.calls == [{"refresh_token": "ref"}]


def test_me_returns_user_with_valid_bearer() -> None:
    user = make_user()
    fake = _Fake(result=user)
    client = next(_client(authenticate=fake))
    resp = client.get("/users/me", headers={"Authorization": "Bearer good-token"})
    assert resp.status_code == 200
    assert resp.json()["username"] == "alice"
    assert fake.calls == [{"access_token": "good-token"}]


def test_me_without_bearer_returns_401() -> None:
    fake = _Fake(result=make_user())
    client = next(_client(authenticate=fake))
    resp = client.get("/users/me")
    assert resp.status_code == 401


def test_me_with_invalid_token_returns_401() -> None:
    fake = _Fake(error=InvalidAccessTokenError())
    client = next(_client(authenticate=fake))
    resp = client.get("/users/me", headers={"Authorization": "Bearer bad"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_token"
