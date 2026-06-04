"""Endpoint tests for GET /workers (platform-admin read surface).

Exercised in-process via TestClient with the registry seeded directly and the
current user faked (NFR-TEST-1, no database, no gRPC). Verifies the
platform-admin gate and the serialised liveness view.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.dependencies import (
    get_current_user,
    get_worker_registry,
)
from tests.fleet.fakes import FakeClock, make_worker
from tests.identity.fakes import make_user

_T0 = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


def _app(*, platform_admin: bool, seed: bool = True) -> object:
    from mc_server_dashboard_api.fleet.adapters.registry import (
        InMemoryWorkerRegistry,
    )

    app = create_app()
    registry = InMemoryWorkerRegistry(
        clock=FakeClock(_T0), heartbeat_timeout=dt.timedelta(seconds=30)
    )
    if seed:
        registry.register(make_worker(at=_T0))
    user = make_user()
    user.is_platform_admin = platform_admin
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_worker_registry] = lambda: registry
    return app


def test_requires_platform_admin() -> None:
    client = next(_client(_app(platform_admin=False)))
    assert client.get("/workers").status_code == 403


def test_lists_registered_worker_with_liveness() -> None:
    client = next(_client(_app(platform_admin=True)))
    resp = client.get("/workers")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["workers"]) == 1
    worker = body["workers"][0]
    assert worker["id"] == "worker-1"
    assert worker["status"] == "online"
    assert worker["version"] == "1.0.0"
    assert worker["capabilities"]["drivers"] == ["host-process"]
    assert worker["capabilities"]["max_servers"] == 4


def test_empty_when_no_workers() -> None:
    client = next(_client(_app(platform_admin=True, seed=False)))
    resp = client.get("/workers")
    assert resp.status_code == 200
    assert resp.json() == {"workers": []}
