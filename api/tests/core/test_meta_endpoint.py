"""Endpoint tests for the /api/meta router (issue #1002).

The meta endpoint reports deployment-wide UI facts before any server exists --
currently whether the game-ingress relay is enabled, which the create form uses
to decide whether to surface the game-port control. Exercised in-process via
FastAPI's TestClient with settings overridden (no real config load).
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.config import Settings
from mc_server_dashboard_api.dependencies import get_current_user, get_settings
from tests.identity.fakes import make_user


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


def _app(*, authenticated: bool = True, relay_enabled: bool = False) -> object:
    app = create_app()
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: make_user()
    settings = Settings.model_validate(
        {
            "relay": (
                {
                    "enabled": True,
                    "credential": "secret",
                    "base_domain": "mc.example.com",
                }
                if relay_enabled
                else {"enabled": False}
            )
        }
    )
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def test_meta_requires_authentication() -> None:
    app = _app(authenticated=False)
    client = next(_client(app))
    resp = client.get("/api/meta")
    assert resp.status_code == 401


def test_meta_reports_relay_enabled() -> None:
    app = _app(relay_enabled=True)
    client = next(_client(app))
    resp = client.get("/api/meta")
    assert resp.status_code == 200
    assert resp.json() == {"relay_enabled": True}


def test_meta_reports_relay_disabled() -> None:
    app = _app(relay_enabled=False)
    client = next(_client(app))
    resp = client.get("/api/meta")
    assert resp.status_code == 200
    assert resp.json() == {"relay_enabled": False}
