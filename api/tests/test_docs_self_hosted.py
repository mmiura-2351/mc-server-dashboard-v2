"""Tests for self-hosted API docs pages (issue #1990).

Verifies that ``/api/docs`` and ``/api/redoc`` load swagger-ui and redoc from
same-origin static files instead of CDN, and that their CSP headers permit the
page scripts to execute.
"""

import base64
import hashlib
import re
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


def _script_src(csp: str) -> str:
    return csp.split("script-src")[1].split(";")[0]


def _img_src(csp: str) -> str:
    return csp.split("img-src")[1].split(";")[0]


def _worker_src(csp: str) -> str:
    return csp.split("worker-src")[1].split(";")[0]


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


def test_swagger_ui_csp_uses_hash_not_unsafe_inline(client: TestClient) -> None:
    """The swagger-ui init script is inline; the docs CSP allows exactly it via
    a SHA-256 hash source, not the broader ``'unsafe-inline'`` (issue #2154)."""
    resp = client.get("/api/docs")
    script_src = _script_src(resp.headers["content-security-policy"])
    assert "'unsafe-inline'" not in script_src
    assert "'sha256-" in script_src


def test_docs_csp_hash_matches_rendered_init_script(client: TestClient) -> None:
    """The ``'sha256-...'`` in the docs CSP must equal the hash the browser
    computes over the exact bytes of the emitted inline ``<script>`` — so a
    future FastAPI bump that changes the init script fails CI here instead of
    silently blocking the page (issue #2154)."""
    resp = client.get("/api/docs")
    match = re.search(r"<script>(.*?)</script>", resp.text, re.DOTALL)
    assert match is not None
    digest = hashlib.sha256(match.group(1).encode()).digest()
    expected = "sha256-" + base64.b64encode(digest).decode()
    script_src = _script_src(resp.headers["content-security-policy"])
    assert f"'{expected}'" in script_src


def test_swagger_ui_favicon_self_hosted(client: TestClient) -> None:
    """The favicon must not point at the CDN default that ``img-src`` blocks;
    a ``data:`` URI (permitted by ``img-src ... data:``) is used (issue #2154)."""
    resp = client.get("/api/docs")
    assert "fastapi.tiangolo.com" not in resp.text
    assert 'href="data:image/svg+xml' in resp.text
    assert "data:" in _img_src(resp.headers["content-security-policy"])


def test_docs_csp_img_src_has_no_modrinth(client: TestClient) -> None:
    """cdn.modrinth.com is irrelevant to docs pages and must be dropped from
    the docs CSP img-src (issue #2154)."""
    resp = client.get("/api/docs")
    assert "cdn.modrinth.com" not in _img_src(resp.headers["content-security-policy"])


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


def test_redoc_csp_uses_hash_not_unsafe_inline(client: TestClient) -> None:
    """ReDoc needs no inline script, but the docs CSP is uniform across both
    pages: hash source, no ``'unsafe-inline'`` (issue #2154)."""
    resp = client.get("/api/redoc")
    script_src = _script_src(resp.headers["content-security-policy"])
    assert "'unsafe-inline'" not in script_src
    assert "'sha256-" in script_src


def test_redoc_favicon_self_hosted(client: TestClient) -> None:
    resp = client.get("/api/redoc")
    assert "fastapi.tiangolo.com" not in resp.text
    assert 'href="data:image/svg+xml' in resp.text


def test_redoc_hides_default_logo(client: TestClient) -> None:
    """ReDoc's default logo fetches from cdn.redoc.ly, which ``img-src``
    correctly blocks. The page must pass ``hide-logo`` so the request is
    never made (issue #2234)."""
    resp = client.get("/api/redoc")
    assert "cdn.redoc.ly" not in resp.text
    assert "hide-logo" in resp.text


def test_docs_csp_worker_src_allows_blob(client: TestClient) -> None:
    """ReDoc creates a search web-worker from a ``blob:`` URL. Without
    ``worker-src ... blob:`` the browser falls back to ``script-src`` and
    blocks it (issue #2234)."""
    resp = client.get("/api/redoc")
    csp = resp.headers["content-security-policy"]
    worker_src = _worker_src(csp)
    assert "blob:" in worker_src


def test_swagger_csp_worker_src_allows_blob(client: TestClient) -> None:
    """The docs CSP is shared; verify worker-src is present on /api/docs
    as well (issue #2234)."""
    resp = client.get("/api/docs")
    csp = resp.headers["content-security-policy"]
    worker_src = _worker_src(csp)
    assert "blob:" in worker_src


def test_non_docs_csp_no_worker_src(client: TestClient) -> None:
    """Non-docs paths must not have the worker-src relaxation."""
    resp = client.get("/api/healthz")
    csp = resp.headers["content-security-policy"]
    assert "worker-src" not in csp


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
