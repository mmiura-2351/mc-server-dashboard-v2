"""Tests for hardening HTTP response headers (issue #635).

Verifies that the security-headers middleware stamps the expected headers on
every response, applies ``Cache-Control: no-store`` only to credential-bearing
endpoints, and emits HSTS only when the request arrives over HTTPS.
"""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Route

from mc_server_dashboard_api.dependencies import get_login, get_refresh_session
from mc_server_dashboard_api.identity.domain.errors import (
    InvalidCredentialsError,
    InvalidRefreshTokenError,
)
from mc_server_dashboard_api.middleware import _NO_STORE_PATHS


class _RejectLogin:
    async def __call__(self, **kwargs: object) -> object:
        raise InvalidCredentialsError


class _RejectRefresh:
    async def __call__(self, **kwargs: object) -> object:
        raise InvalidRefreshTokenError


@pytest.fixture
def client(shared_app: FastAPI) -> Iterator[TestClient]:
    app = shared_app
    app.dependency_overrides.clear()
    # Override auth use cases so the endpoints respond without a database.
    app.dependency_overrides[get_login] = lambda: _RejectLogin()
    app.dependency_overrides[get_refresh_session] = lambda: _RejectRefresh()
    with TestClient(app) as c:
        yield c


# -- (a) Headers present on a normal API response --


def test_csp_header_present(client: TestClient) -> None:
    resp = client.get("/api/healthz")
    csp = resp.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_x_frame_options_deny(client: TestClient) -> None:
    resp = client.get("/api/healthz")
    assert resp.headers["x-frame-options"] == "DENY"


def test_x_content_type_options_nosniff(client: TestClient) -> None:
    resp = client.get("/api/healthz")
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_referrer_policy_present(client: TestClient) -> None:
    resp = client.get("/api/healthz")
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_permissions_policy_present(client: TestClient) -> None:
    resp = client.get("/api/healthz")
    policy = resp.headers["permissions-policy"]
    assert "camera=()" in policy
    assert "microphone=()" in policy
    assert "geolocation=()" in policy


# -- (b) Cache-Control: no-store on auth/me endpoints but NOT on generic --


def test_cache_control_no_store_on_auth_login(client: TestClient) -> None:
    resp = client.post("/api/auth/login", json={"username": "x", "password": "y"})
    # The endpoint returns 401 (fake rejects all); the middleware still runs.
    assert resp.headers.get("cache-control") == "no-store"


def test_cache_control_no_store_on_auth_refresh(client: TestClient) -> None:
    resp = client.post("/api/auth/refresh", json={"refresh_token": "x"})
    assert resp.headers.get("cache-control") == "no-store"


def test_cache_control_no_store_on_auth_session(client: TestClient) -> None:
    resp = client.post("/api/auth/session")
    # Returns 401 (no refresh cookie); the middleware still runs.
    assert resp.headers.get("cache-control") == "no-store"


def test_cache_control_no_store_on_users_me(client: TestClient) -> None:
    resp = client.get("/api/users/me")
    # Returns 401 (no Bearer token); the middleware still runs.
    assert resp.headers.get("cache-control") == "no-store"


def test_cache_control_no_store_on_users_me_sessions(client: TestClient) -> None:
    resp = client.get("/api/users/me/sessions")
    # Returns 401 (no Bearer token); the middleware still runs.
    assert resp.headers.get("cache-control") == "no-store"


def test_cache_control_absent_on_generic_endpoint(client: TestClient) -> None:
    resp = client.get("/api/healthz")
    assert "cache-control" not in resp.headers


@pytest.mark.parametrize("path", sorted(_NO_STORE_PATHS))
def test_no_store_path_names_a_live_route(shared_app: FastAPI, path: str) -> None:
    """Every entry in the set is a path the app still routes (issue #2563).

    The middleware compares ``request.url.path`` against the frozenset, so it
    stamps the header on a 404 exactly as readily as on the endpoint the entry
    was written for. That makes the five per-path tests above blind to the
    failure they look like they cover: rename ``/api/auth/login`` and its test
    stays green on the 404 while the real endpoint silently loses ``no-store``.
    Checking the entry against the route table is what reddens on a rename, a
    new path prefix, or a route that moved to a different path.
    """

    declared = {route.path for route in shared_app.routes if isinstance(route, Route)}
    assert path in declared, (
        f"{path} is in _NO_STORE_PATHS but no route declares it -- a renamed or "
        "re-prefixed endpoint is no longer getting Cache-Control: no-store"
    )


# -- (c) HSTS appears only when forwarded proto is HTTPS --


def test_hsts_present_when_forwarded_proto_https(client: TestClient) -> None:
    resp = client.get("/api/healthz", headers={"X-Forwarded-Proto": "https"})
    hsts = resp.headers.get("strict-transport-security")
    assert hsts is not None
    assert "max-age=31536000" in hsts
    assert "includeSubDomains" in hsts


def test_hsts_absent_on_plain_http(client: TestClient) -> None:
    resp = client.get("/api/healthz")
    assert "strict-transport-security" not in resp.headers
