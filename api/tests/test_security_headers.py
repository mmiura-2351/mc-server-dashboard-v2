"""Tests for hardening HTTP response headers (issue #635).

Verifies that the security-headers middleware stamps the expected headers on
every response and emits HSTS only when the request arrives over HTTPS.
``Cache-Control: no-store`` is not the middleware's to stamp (issue #2587): the
routes that need it declare it themselves, and those pins live beside each
route's own tests.
"""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Route

from mc_server_dashboard_api.middleware import _DOCS_PATHS


@pytest.fixture
def client(shared_app: FastAPI) -> Iterator[TestClient]:
    app = shared_app
    app.dependency_overrides.clear()
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


# -- (b) The middleware stamps no Cache-Control of its own --


def test_cache_control_absent_on_generic_endpoint(client: TestClient) -> None:
    # No route on this path declares a caching policy, and the middleware adds
    # none: a blanket Cache-Control here would decide the policy for every route
    # that has not stated one (issue #2587).
    resp = client.get("/api/healthz")
    assert "cache-control" not in resp.headers


@pytest.mark.parametrize("path", sorted(_DOCS_PATHS))
def test_docs_path_names_a_live_route(shared_app: FastAPI, path: str) -> None:
    """Every entry in the docs set is a path the app still routes (issue #2587).

    ``_DOCS_PATHS`` selects the relaxed CSP that allows the swagger init script
    by hash, and it is matched by exact path, so nothing in the routers signals
    that renaming a docs route drops the relaxation. That reapplies the strict
    ``script-src 'self'``, which blocks that script: the page still returns 200
    and only the browser console says why it renders blank. Checking the entry
    against the route table is what reddens on the rename.
    """

    declared = {route.path for route in shared_app.routes if isinstance(route, Route)}
    assert path in declared, (
        f"{path} is in _DOCS_PATHS but no route declares it -- a renamed docs "
        "page is back under the strict CSP, which blocks its init script"
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
