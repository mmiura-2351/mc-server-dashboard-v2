"""The OpenAPI schema and docs pages are switchable, off by default (issue #2568).

``cloudflared`` forwards the whole public hostname to ``api:8000``, so every route
on the HTTP port is on the internet (SECURITY.md Section 5). The schema and the
two docs pages publish the complete route inventory to an unauthenticated
caller; ``docs.enabled`` decides whether they are mounted at all, and a
deployment that sets nothing mounts none of them.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.config import DocsSettings
from tests.test_compose_log_forwarding import _compose_service

_DOCS_SURFACE = (
    "/api/openapi.json",
    "/api/docs",
    "/api/redoc",
    "/api/docs-assets/swagger-ui.css",
)


def _client(monkeypatch: pytest.MonkeyPatch, enabled: str | None) -> TestClient:
    if enabled is None:
        monkeypatch.delenv("MCD_API_DOCS__ENABLED", raising=False)
    else:
        monkeypatch.setenv("MCD_API_DOCS__ENABLED", enabled)
    return TestClient(create_app())


@pytest.fixture
def default_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _client(monkeypatch, None) as client:
        yield client


@pytest.fixture
def enabled_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _client(monkeypatch, "true") as client:
        yield client


@pytest.mark.parametrize("path", _DOCS_SURFACE)
def test_docs_surface_is_not_mounted_by_default(
    default_client: TestClient, path: str
) -> None:
    resp = default_client.get(path)

    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize("path", _DOCS_SURFACE)
def test_docs_surface_is_mounted_when_enabled(
    enabled_client: TestClient, path: str
) -> None:
    resp = enabled_client.get(path)

    assert resp.status_code == 200


def test_compose_forwards_docs_enabled_with_the_code_default() -> None:
    """The operator knob reaches the container, defaulted to the code default.

    Unforwarded, ``MCD_API_DOCS__ENABLED=true`` in ``.env`` would be silently
    inert on the only way this project is deployed (the issue #2794 class).
    """

    default = str(DocsSettings().enabled).lower()

    assert (
        f'MCD_API_DOCS__ENABLED: "${{MCD_API_DOCS__ENABLED:-{default}}}"'
        in _compose_service("api")
    )
