"""Endpoint tests for the /workers platform-admin surface.

Exercised in-process via TestClient with the registry seeded directly and the
current user faked (NFR-TEST-1, no database, no gRPC). Verifies the
platform-admin gate, the serialised liveness view, and the drain set/clear
endpoints (FR-WRK-5).
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
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from tests.fleet.fakes import FakeClock, make_worker
from tests.identity.fakes import make_user

_T0 = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


def _app(
    *, platform_admin: bool, seed: bool = True
) -> tuple[object, InMemoryWorkerRegistry]:
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
    return app, registry


def test_requires_platform_admin() -> None:
    app, _ = _app(platform_admin=False)
    client = next(_client(app))
    assert client.get("/workers").status_code == 403


def test_lists_registered_worker_with_liveness() -> None:
    app, _ = _app(platform_admin=True)
    client = next(_client(app))
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
    app, _ = _app(platform_admin=True, seed=False)
    client = next(_client(app))
    resp = client.get("/workers")
    assert resp.status_code == 200
    assert resp.json() == {"workers": []}


def test_set_drain_requires_platform_admin() -> None:
    app, _ = _app(platform_admin=False)
    client = next(_client(app))
    assert client.put("/workers/worker-1/drain").status_code == 403


def test_clear_drain_requires_platform_admin() -> None:
    app, _ = _app(platform_admin=False)
    client = next(_client(app))
    assert client.delete("/workers/worker-1/drain").status_code == 403


def test_set_drain_marks_worker_draining_in_listing() -> None:
    app, _ = _app(platform_admin=True)
    client = next(_client(app))

    assert client.put("/workers/worker-1/drain").status_code == 204

    worker = client.get("/workers").json()["workers"][0]
    assert worker["status"] == "draining"


def test_clear_drain_returns_worker_to_online() -> None:
    app, registry = _app(platform_admin=True)
    registry.set_draining(make_worker(at=_T0).id, True)
    client = next(_client(app))

    assert client.delete("/workers/worker-1/drain").status_code == 204

    worker = client.get("/workers").json()["workers"][0]
    assert worker["status"] == "online"
