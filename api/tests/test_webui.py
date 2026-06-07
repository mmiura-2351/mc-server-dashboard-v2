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


@pytest.mark.parametrize(
    "deep_link",
    [
        "/communities/11111111-1111-1111-1111-111111111111",
        "/communities/11111111-1111-1111-1111-111111111111"
        "/servers/22222222-2222-2222-2222-222222222222",
        "/communities/11111111-1111-1111-1111-111111111111/servers/new",
    ],
)
def test_colliding_deep_links_reload_to_spa(
    webui_client: TestClient, deep_link: str
) -> None:
    # Regression for issue #498: these three SPA routes used to collide with API
    # GET routes on a hard reload and return JSON. With the API namespaced under
    # /api, a non-/api browser navigation (Accept: text/html, no auth) now falls
    # through to the SPA index.
    resp = webui_client.get(deep_link, headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert "spa" in resp.text


def test_api_route_takes_precedence(webui_client: TestClient) -> None:
    # /api/healthz is a real API route and must NOT be shadowed by the SPA mount.
    resp = webui_client.get("/api/healthz")
    assert resp.status_code == 200
    assert resp.json()["database_reachable"] in (True, False)


def test_openapi_takes_precedence(webui_client: TestClient) -> None:
    # The generated schema must keep its real JSON, not the SPA fallback.
    resp = webui_client.get("/api/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["openapi"].startswith("3.")


def test_unmatched_api_path_is_404_problem_not_spa(webui_client: TestClient) -> None:
    # Regression for issue #567: an unmatched /api/* path must return an RFC 9457
    # problem 404, never the SPA index.html. The SPA fallback is reserved for
    # non-/api paths ("everything non-/api serves the SPA").
    resp = webui_client.get("/api/nonexistent")
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["reason"] == "not_found"


def test_spa_route_still_serves_index(webui_client: TestClient) -> None:
    # The counterpart to the /api 404: a non-/api path still falls back to the
    # SPA index.html with 200 text/html (issue #567).
    resp = webui_client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "spa" in resp.text


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
