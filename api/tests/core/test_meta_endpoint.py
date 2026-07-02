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


def _app(
    *,
    authenticated: bool = True,
    relay_enabled: bool = False,
    bedrock_enabled: bool = False,
) -> object:
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
                    "bedrock_enabled": bedrock_enabled,
                }
                if relay_enabled
                else {"enabled": False, "bedrock_enabled": bedrock_enabled}
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
    assert resp.json()["relay_enabled"] is True


def test_meta_reports_relay_disabled() -> None:
    app = _app(relay_enabled=False)
    client = next(_client(app))
    resp = client.get("/api/meta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["relay_enabled"] is False


# --- Memory-limit config in /meta (issue #1069) ---


def test_meta_reports_memory_limit_defaults_as_null() -> None:
    app = _app()
    client = next(_client(app))
    resp = client.get("/api/meta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["default_memory_limit_mb"] is None
    assert body["max_memory_limit_mb"] is None


def test_meta_reports_configured_memory_limits() -> None:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    settings = Settings.model_validate(
        {
            "memory_limit": {"default_mb": 2048, "max_mb": 8192},
        }
    )
    app.dependency_overrides[get_settings] = lambda: settings
    client = next(_client(app))
    resp = client.get("/api/meta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["default_memory_limit_mb"] == 2048
    assert body["max_memory_limit_mb"] == 8192


# --- Bedrock capability in /meta (issue #1541) ---


def test_meta_reports_bedrock_enabled_with_relay_and_capability() -> None:
    app = _app(relay_enabled=True, bedrock_enabled=True)
    client = next(_client(app))
    resp = client.get("/api/meta")
    assert resp.status_code == 200
    assert resp.json()["bedrock_enabled"] is True


def test_meta_reports_bedrock_disabled_by_default() -> None:
    app = _app(relay_enabled=True)
    client = next(_client(app))
    resp = client.get("/api/meta")
    assert resp.status_code == 200
    assert resp.json()["bedrock_enabled"] is False


def test_meta_reports_bedrock_disabled_without_relay() -> None:
    # The capability flag alone is not enough: the Bedrock path rides the relay.
    app = _app(relay_enabled=False, bedrock_enabled=True)
    client = next(_client(app))
    resp = client.get("/api/meta")
    assert resp.status_code == 200
    assert resp.json()["bedrock_enabled"] is False
