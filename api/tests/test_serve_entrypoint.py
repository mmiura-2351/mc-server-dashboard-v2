"""The server entry point binds from configuration (issue #2585).

``api/Dockerfile`` used to run ``uvicorn ... --host 0.0.0.0 --port 8000``, which
made ``server.host`` and ``server.http_port`` — declared, typed and documented
settings (CONFIGURATION.md Section 5.1) — dead config in the container image, the
only way this project is deployed. Setting ``MCD_API_SERVER__HTTP_PORT`` produced
no error, no warning and no effect.

These pin the three halves of the fixed shape: the entry point takes the bind
from the settings, the image's ``CMD`` carries no bind arguments of its own, and
``compose.yaml`` derives everything container-side from the same key instead of
restating ``8000``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from mc_server_dashboard_api import serve

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "api" / "Dockerfile"
_COMPOSE_FILE = _REPO_ROOT / "compose.yaml"


def _capture_uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run ``serve.main()`` with ``uvicorn.run`` stubbed, returning its call."""

    captured: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    serve.main()
    return captured


def test_main_binds_the_configured_host_and_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCD_API_SERVER__HOST", "127.0.0.1")
    monkeypatch.setenv("MCD_API_SERVER__HTTP_PORT", "18585")

    captured = _capture_uvicorn_run(monkeypatch)

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 18585


def test_main_hands_uvicorn_a_working_app_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bind values and the app must come from ONE settings load.

    ``serve.main`` closes over the settings it took the bind from, so uvicorn
    builds the app from exactly those values. It is passed as a *factory* on
    purpose: uvicorn configures logging in ``Config.__init__`` and calls the
    factory later (``Config.load``), so ``create_app``'s own
    ``configure_logging`` still lands last — the ordering the ``uvicorn`` CLI
    form gave. Building the app here and passing the instance would invert it.
    """

    captured = _capture_uvicorn_run(monkeypatch)

    assert captured["factory"] is True
    assert isinstance(captured["app"](), FastAPI)


def test_main_defaults_to_the_image_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing set the entry binds ``0.0.0.0:8000``.

    This is what keeps the compose publish working out of the box: the published
    port below targets the container-side port, and a default of loopback would
    leave it unreachable from the host.
    """

    monkeypatch.delenv("MCD_API_SERVER__HOST", raising=False)
    monkeypatch.delenv("MCD_API_SERVER__HTTP_PORT", raising=False)

    captured = _capture_uvicorn_run(monkeypatch)

    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 8000


def test_image_entry_command_carries_no_bind_arguments() -> None:
    """The image's ``CMD`` must not restate the bind (the #2585 defect itself)."""

    cmd = next(
        line for line in _DOCKERFILE.read_text().splitlines() if line.startswith("CMD ")
    )

    assert "--host" not in cmd
    assert "--port" not in cmd
    assert "mc_server_dashboard_api.serve" in cmd


def _compose_api_service() -> str:
    """The ``api`` service block of the shipped ``compose.yaml``.

    Scoped to that service so a same-named key under another service can never
    be picked up by a whole-file search (the reason given in issue #2596).
    """

    lines = _COMPOSE_FILE.read_text().splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith("  api:"))
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].strip() and not lines[i].startswith("   ")
        ),
        len(lines),
    )
    return "".join(lines[start:end])


def test_compose_forwards_the_server_bind_settings() -> None:
    """Both keys reach the container, defaulted to the image bind.

    Unforwarded is the same failure as hardcoded: the operator sets the
    documented variable in ``.env`` and nothing happens. Mirrors the metrics
    listener's own forwarding (issue #2565).
    """

    api = _compose_api_service()

    assert 'MCD_API_SERVER__HOST: "${MCD_API_SERVER__HOST:-0.0.0.0}"' in api
    assert 'MCD_API_SERVER__HTTP_PORT: "${MCD_API_SERVER__HTTP_PORT:-8000}"' in api


def test_compose_derives_the_container_side_port_from_the_setting() -> None:
    """Nothing container-side may restate ``8000`` independently.

    The publish target, the healthcheck probe and the two internal base URLs all
    address the port the app binds; a literal in any of them turns a changed
    ``MCD_API_SERVER__HTTP_PORT`` into an unreachable, unhealthy or
    unreachable-to-the-worker API.
    """

    api = _compose_api_service()
    container_port = "${MCD_API_SERVER__HTTP_PORT:-8000}"

    publish = f'"${{API_HTTP_BIND_IP:-127.0.0.1}}:${{API_HTTP_PORT}}:{container_port}"'
    assert publish in api
    assert f"http://localhost:{container_port}/api/healthz" in api
    assert (
        f"MCD_API_SERVER__PUBLIC_BASE_URL: "
        f"${{MCD_API_SERVER__PUBLIC_BASE_URL:-http://api:{container_port}}}" in api
    )
    assert (
        f"MCD_API_SERVER__DATA_PLANE_BASE_URL: "
        f"${{MCD_API_SERVER__DATA_PLANE_BASE_URL:-http://api:{container_port}}}" in api
    )
