"""Endpoint tests for the /workers platform-admin surface.

Exercised in-process via TestClient with the registry seeded directly and the
current user faked (NFR-TEST-1, no database, no gRPC). Verifies the
platform-admin gate, the serialised liveness view, and the drain set/clear
endpoints (FR-WRK-5).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mc_server_dashboard_api.dependencies import (
    get_current_user,
    get_set_worker_drain,
    get_worker_registry,
)
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.application.set_worker_drain import SetWorkerDrain
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from tests.fleet.fakes import FakeClock, make_worker
from tests.identity.fakes import make_user
from tests.servers.fakes import FakeClock as ServersFakeClock
from tests.servers.fakes import FakeUnitOfWork

_T0 = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_WORKER_UUID = str(uuid.uuid4())

_shared_app: FastAPI


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    global _shared_app
    _shared_app = shared_app


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


def _app(
    *, platform_admin: bool, seed: bool = True
) -> tuple[object, InMemoryWorkerRegistry]:
    # Reuse the per-worker shared app; clear overrides on entry so a helper called
    # twice in one test starts clean (the shared_app wrapper clears between tests).
    app = _shared_app
    app.dependency_overrides.clear()
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
    assert client.get("/api/workers").status_code == 403


def test_lists_registered_worker_with_liveness() -> None:
    app, _ = _app(platform_admin=True)
    client = next(_client(app))
    resp = client.get("/api/workers")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["workers"]) == 1
    worker = body["workers"][0]
    assert worker["id"] == "worker-1"
    assert worker["status"] == "online"
    assert worker["version"] == "1.0.0"
    assert worker["capabilities"]["drivers"] == ["container"]
    assert worker["capabilities"]["max_servers"] == 4


def test_empty_when_no_workers() -> None:
    app, _ = _app(platform_admin=True, seed=False)
    client = next(_client(app))
    resp = client.get("/api/workers")
    assert resp.status_code == 200
    assert resp.json() == {"workers": []}


def test_set_drain_requires_platform_admin() -> None:
    app, _ = _app(platform_admin=False)
    client = next(_client(app))
    assert client.put("/api/workers/worker-1/drain").status_code == 403


def test_clear_drain_requires_platform_admin() -> None:
    app, _ = _app(platform_admin=False)
    client = next(_client(app))
    assert client.delete("/api/workers/worker-1/drain").status_code == 403


def test_set_drain_marks_worker_draining_in_listing() -> None:
    app, registry = _app(platform_admin=True, seed=False)
    registry.register(make_worker(worker_id=_WORKER_UUID, at=_T0))
    use_case = SetWorkerDrain(
        registry=registry, uow=FakeUnitOfWork(), clock=ServersFakeClock(_T0)
    )
    _shared_app.dependency_overrides[get_set_worker_drain] = lambda: use_case
    client = next(_client(app))

    resp = client.put(f"/api/workers/{_WORKER_UUID}/drain")
    assert resp.status_code == 200
    # No assigned servers in this in-memory fixture, so the count is zero; the
    # response shape (servers_stopped) is what this asserts.
    assert resp.json() == {"servers_stopped": 0}

    worker = client.get("/api/workers").json()["workers"][0]
    assert worker["status"] == "draining"


def test_clear_drain_returns_worker_to_online() -> None:
    app, registry = _app(platform_admin=True, seed=False)
    registry.register(make_worker(worker_id=_WORKER_UUID, at=_T0))
    registry.set_draining(WorkerId(_WORKER_UUID), True)
    use_case = SetWorkerDrain(
        registry=registry, uow=FakeUnitOfWork(), clock=ServersFakeClock(_T0)
    )
    _shared_app.dependency_overrides[get_set_worker_drain] = lambda: use_case
    client = next(_client(app))

    assert client.delete(f"/api/workers/{_WORKER_UUID}/drain").status_code == 204

    worker = client.get("/api/workers").json()["workers"][0]
    assert worker["status"] == "online"


def test_set_drain_unknown_worker_is_404() -> None:
    app, _ = _app(platform_admin=True, seed=False)
    client = next(_client(app))
    assert client.put("/api/workers/ghost/drain").status_code == 404


def test_clear_drain_unknown_worker_is_404() -> None:
    app, _ = _app(platform_admin=True, seed=False)
    client = next(_client(app))
    assert client.delete("/api/workers/ghost/drain").status_code == 404
