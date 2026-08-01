"""The observability listener the API lifespan stands up (issue #2565).

The Prometheus exposition moved off the public API port onto its own listener,
because the bundled Cloudflare tunnel forwards the whole hostname to `api:8000`
and so published every path the API served. The listener is opt-in
(`metrics.enabled`, default false), mirroring the relay (RELAY.md Section 13).

These tests bind a real ephemeral port on loopback and scrape it over HTTP —
the point of the change is that the exposition is served by a *separate socket*,
which only a real socket can demonstrate.
"""

import signal
import socket
from collections.abc import Iterator

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.observability import _address_family, _EmbeddedServer


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


def test_bind_failure_leaves_the_api_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    """A taken metrics port must not take the API down (#2565).

    9090 is Prometheus's own default port and the listener binds every
    interface, so a collision on a bare-metal host is ordinary, not exotic.
    """

    occupied = socket.create_server(("127.0.0.1", 0))
    try:
        monkeypatch.setenv("MCD_API_METRICS__ENABLED", "true")
        monkeypatch.setenv("MCD_API_METRICS__HOST", "127.0.0.1")
        monkeypatch.setenv("MCD_API_METRICS__PORT", str(occupied.getsockname()[1]))
        app = create_app()
        with TestClient(app) as client:
            # No listener, but the API itself came up and serves.
            assert app.state.metrics_listener is None
            assert client.get("/api/healthz").status_code in (200, 503)
    finally:
        occupied.close()


# --- the embedded server must not touch process signal handling (#2565) ---
#
# The lifespan owns this listener's shutdown; the OUTER uvicorn server owns the
# process's SIGINT/SIGTERM. Swapping ``_EmbeddedServer`` back for a plain
# ``uvicorn.Server`` breaks nothing any other test observes — the container's
# SIGTERM just silently starts reaching the inner listener instead of the API's
# own server. These two pin the difference. They run on pytest's main thread,
# which is where ``capture_signals`` actually does something (it returns early
# off the main thread, which is why a TestClient-driven lifespan cannot see it).


def test_embedded_server_leaves_process_signal_handlers_alone() -> None:
    config = uvicorn.Config(FastAPI())
    original = signal.getsignal(signal.SIGTERM)
    with _EmbeddedServer(config).capture_signals():
        assert signal.getsignal(signal.SIGTERM) == original


def test_base_uvicorn_server_shadows_the_handler() -> None:
    # The control: without the override the inner server takes SIGTERM over.
    # If this ever stops holding, the override may no longer be needed.
    config = uvicorn.Config(FastAPI())
    original = signal.getsignal(signal.SIGTERM)
    server = uvicorn.Server(config)
    with server.capture_signals():
        assert signal.getsignal(signal.SIGTERM) == server.handle_exit
        assert signal.getsignal(signal.SIGTERM) != original
    assert signal.getsignal(signal.SIGTERM) == original


def test_address_family_follows_the_host_form() -> None:
    # ``socket.create_server`` defaults to AF_INET, so an IPv6 metrics.host
    # would raise and be swallowed as a non-fatal bind failure — the listener
    # would silently never start. Sniff for ``:`` exactly as uvicorn does.
    assert _address_family("0.0.0.0") == socket.AF_INET
    assert _address_family("127.0.0.1") == socket.AF_INET
    assert _address_family("::") == socket.AF_INET6
    assert _address_family("::1") == socket.AF_INET6
    assert _address_family("fd00::1") == socket.AF_INET6
