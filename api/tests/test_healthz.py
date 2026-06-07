"""Endpoint tests for GET /healthz with the DatabasePing Port faked.

The HTTP boundary is exercised in-process via FastAPI's TestClient; the database
Port is overridden with a fake (NFR-TEST-1), so no real database is touched.
DB-down reports degraded (200 body ok=false) rather than crashing.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.core.domain.health import DatabasePing
from mc_server_dashboard_api.dependencies import get_database_ping


class _FakePing(DatabasePing):
    def __init__(self, *, reachable: bool) -> None:
        self._reachable = reachable

    async def is_reachable(self) -> bool:
        return self._reachable


def _client(*, reachable: bool) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_database_ping] = lambda: _FakePing(reachable=reachable)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def healthy_client() -> Iterator[TestClient]:
    yield from _client(reachable=True)


@pytest.fixture
def degraded_client() -> Iterator[TestClient]:
    yield from _client(reachable=False)


def test_healthz_ok_when_database_reachable(healthy_client: TestClient) -> None:
    resp = healthy_client.get("/api/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "database_reachable": True}


def test_healthz_degraded_when_database_down(degraded_client: TestClient) -> None:
    resp = degraded_client.get("/api/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "database_reachable": False}


def test_healthz_sets_correlation_id_header(healthy_client: TestClient) -> None:
    resp = healthy_client.get("/api/healthz")
    assert resp.headers.get("X-Correlation-ID")
