"""Endpoint tests for GET /readyz (issue #282).

The DB ping and control-plane readiness Ports are overridden with fakes
(NFR-TEST-1) so no real database or gRPC server is touched. Readiness returns 200
with per-component booleans when every critical component is ready, and 503 with
the same shape when any is not — /healthz stays the cheap liveness probe.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.core.domain.health import DatabasePing
from mc_server_dashboard_api.core.domain.readiness import ControlPlaneReadiness
from mc_server_dashboard_api.dependencies import (
    get_control_plane_readiness,
    get_database_ping,
)


class _FakePing(DatabasePing):
    def __init__(self, *, reachable: bool) -> None:
        self._reachable = reachable

    async def is_reachable(self) -> bool:
        return self._reachable


class _FakeControlPlane(ControlPlaneReadiness):
    def __init__(self, *, ready: bool) -> None:
        self._ready = ready

    def is_ready(self) -> bool:
        return self._ready


def _client(*, db_ready: bool, cp_ready: bool) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_database_ping] = lambda: _FakePing(reachable=db_ready)
    app.dependency_overrides[get_control_plane_readiness] = lambda: _FakeControlPlane(
        ready=cp_ready
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def ready_client() -> Iterator[TestClient]:
    yield from _client(db_ready=True, cp_ready=True)


def test_readyz_ok_when_all_components_ready(ready_client: TestClient) -> None:
    resp = ready_client.get("/api/readyz")
    assert resp.status_code == 200
    assert resp.json() == {
        "ready": True,
        "components": {"database": True, "control_plane": True},
    }


def test_readyz_503_when_database_down() -> None:
    for client in _client(db_ready=False, cp_ready=True):
        resp = client.get("/api/readyz")
        assert resp.status_code == 503
        body = resp.json()
        assert body["ready"] is False
        assert body["components"] == {"database": False, "control_plane": True}


def test_readyz_503_when_control_plane_not_started() -> None:
    for client in _client(db_ready=True, cp_ready=False):
        resp = client.get("/api/readyz")
        assert resp.status_code == 503
        assert resp.json()["components"]["control_plane"] is False
