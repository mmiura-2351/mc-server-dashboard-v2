"""Serving the built Web UI from the API origin (WEBUI_SPEC 7.7, issue #490).

Covers the four behaviours of the conditional SPA mount: asset serving, SPA
fallback for deep links/reloads, API-route precedence over the catch-all mount,
and no mount (so dev + tests are unaffected) when ``webui.dist_dir`` is unset.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app


@pytest.fixture
def dist_dir(tmp_path: Path) -> Path:
    """A minimal built-SPA directory: index.html + a fingerprinted asset."""

    (tmp_path / "index.html").write_text("<!doctype html><title>spa</title>")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app-abc123.js").write_text("console.log('app')")
    return tmp_path


@pytest.fixture
def webui_client(
    monkeypatch: pytest.MonkeyPatch, dist_dir: Path
) -> Iterator[TestClient]:
    monkeypatch.setenv("MCD_API_WEBUI__DIST_DIR", str(dist_dir))
    app = create_app()
    with TestClient(app) as client:
        yield client


def test_serves_index_at_root(webui_client: TestClient) -> None:
    resp = webui_client.get("/")
    assert resp.status_code == 200
    assert "spa" in resp.text


def test_serves_fingerprinted_asset(webui_client: TestClient) -> None:
    resp = webui_client.get("/assets/app-abc123.js")
    assert resp.status_code == 200
    assert "console.log" in resp.text
    assert "javascript" in resp.headers["content-type"]


def test_deep_link_falls_back_to_index(webui_client: TestClient) -> None:
    # A client-side route that matches no API route nor asset reloads to
    # index.html (200), so the SPA router can render it.
    resp = webui_client.get("/login")
    assert resp.status_code == 200
    assert "spa" in resp.text


def test_api_route_takes_precedence(webui_client: TestClient) -> None:
    # /healthz is a real API route and must NOT be shadowed by the SPA mount.
    resp = webui_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["database_reachable"] in (True, False)


def test_openapi_takes_precedence(webui_client: TestClient) -> None:
    # The generated schema must keep its real JSON, not the SPA fallback.
    resp = webui_client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["openapi"].startswith("3.")


def test_no_mount_when_dist_dir_unset() -> None:
    # Default (unset): root is not served, so dev (Vite proxy) and tests see the
    # bare API. FastAPI returns 404 for the unrouted path.
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 404


def test_missing_dist_dir_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCD_API_WEBUI__DIST_DIR", "/no/such/dist")
    with pytest.raises(ValueError, match="not a directory"):
        create_app()


def test_dist_dir_without_index_fails_fast(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCD_API_WEBUI__DIST_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="no index.html"):
        create_app()
