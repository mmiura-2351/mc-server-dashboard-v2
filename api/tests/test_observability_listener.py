"""The observability listener the API lifespan stands up (issue #2565).

The Prometheus exposition moved off the public API port onto its own listener,
because the bundled Cloudflare tunnel forwards the whole hostname to `api:8000`
and so published every path the API served. The listener is opt-in
(`metrics.enabled`, default false), mirroring the relay (RELAY.md Section 17).

These tests bind a real ephemeral port on loopback and scrape it over HTTP —
the point of the change is that the exposition is served by a *separate socket*,
which only a real socket can demonstrate.
"""

from collections.abc import Iterator

import httpx2
import pytest
from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app


@pytest.fixture
def _loopback_listener(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Enable the listener on an OS-assigned loopback port for the test."""

    monkeypatch.setenv("MCD_API_METRICS__ENABLED", "true")
    monkeypatch.setenv("MCD_API_METRICS__HOST", "127.0.0.1")
    monkeypatch.setenv("MCD_API_METRICS__PORT", "0")
    yield


def test_listener_is_not_started_when_disabled() -> None:
    # Default posture: no second port is bound at all (CONFIGURATION.md 5.10).
    app = create_app()
    with TestClient(app):
        assert app.state.metrics_listener is None


@pytest.mark.usefixtures("_loopback_listener")
def test_listener_serves_the_exposition_on_its_own_port() -> None:
    app = create_app()
    with TestClient(app):
        listener = app.state.metrics_listener
        assert listener is not None
        resp = httpx2.get(f"http://127.0.0.1:{listener.port}/metrics", timeout=10.0)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        assert "http_requests_total" in resp.text


@pytest.mark.usefixtures("_loopback_listener")
def test_listener_stops_with_the_lifespan() -> None:
    app = create_app()
    with TestClient(app):
        port = app.state.metrics_listener.port
    # Past the lifespan the socket is closed: nothing answers on that port.
    with pytest.raises(httpx2.ConnectError):
        httpx2.get(f"http://127.0.0.1:{port}/metrics", timeout=10.0)
