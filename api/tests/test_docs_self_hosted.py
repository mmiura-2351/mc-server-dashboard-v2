"""Tests for self-hosted API docs pages (issue #1990).

Verifies that ``/api/docs`` and ``/api/redoc`` load swagger-ui and redoc from
same-origin static files instead of CDN, and that their CSP headers permit the
page scripts to execute.
"""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(shared_app: FastAPI) -> Iterator[TestClient]:
    app = shared_app
    app.dependency_overrides.clear()
    with TestClient(app) as c:
        yield c


# -- Swagger UI (/api/docs) --


def test_swagger_ui_loads_js_from_self(client: TestClient) -> None:
    resp = client.get("/api/docs")
    assert resp.status_code == 200
    body = resp.text
    assert "/api/docs-assets/swagger-ui-bundle.js" in body
    # Must NOT reference the CDN.
    assert "cdn.jsdelivr.net" not in body


def test_swagger_ui_loads_css_from_self(client: TestClient) -> None:
    resp = client.get("/api/docs")
    body = resp.text
    assert "/api/docs-assets/swagger-ui.css" in body


def test_swagger_ui_csp_permits_inline_script(client: TestClient) -> None:
    """The swagger-ui init script is inline; the CSP must allow it."""
    resp = client.get("/api/docs")
    csp = resp.headers["content-security-policy"]
    assert "'unsafe-inline'" in csp.split("script-src")[1].split(";")[0]


def test_swagger_ui_no_google_fonts(client: TestClient) -> None:
    resp = client.get("/api/docs")
    assert "fonts.googleapis.com" not in resp.text


# -- ReDoc (/api/redoc) --


def test_redoc_loads_js_from_self(client: TestClient) -> None:
    resp = client.get("/api/redoc")
    assert resp.status_code == 200
    body = resp.text
    assert "/api/docs-assets/redoc.standalone.js" in body
    assert "cdn.jsdelivr.net" not in body


def test_redoc_no_google_fonts(client: TestClient) -> None:
    resp = client.get("/api/redoc")
    assert "fonts.googleapis.com" not in resp.text


def test_redoc_csp_permits_inline_script(client: TestClient) -> None:
    """ReDoc itself needs no inline script, but keep the docs CSP uniform."""
    resp = client.get("/api/redoc")
    csp = resp.headers["content-security-policy"]
    assert "'unsafe-inline'" in csp.split("script-src")[1].split(";")[0]


# -- Non-docs paths retain the strict CSP --


def test_non_docs_csp_no_unsafe_inline_in_script_src(client: TestClient) -> None:
    resp = client.get("/api/healthz")
    csp = resp.headers["content-security-policy"]
    script_src = csp.split("script-src")[1].split(";")[0]
    assert "'unsafe-inline'" not in script_src


# -- Static assets are accessible --


def test_swagger_ui_js_asset_accessible(client: TestClient) -> None:
    resp = client.get("/api/docs-assets/swagger-ui-bundle.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_swagger_ui_css_asset_accessible(client: TestClient) -> None:
    resp = client.get("/api/docs-assets/swagger-ui.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]


def test_redoc_js_asset_accessible(client: TestClient) -> None:
    resp = client.get("/api/docs-assets/redoc.standalone.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
