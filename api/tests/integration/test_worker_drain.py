"""Drain intent survives an API restart (issue #3535, FR-WRK-5).

Drain used to live only in the in-memory registry, so a Worker drained before an
API restart re-registered ONLINE and placeable. These drive the real app against
a real database: each ``create_app`` lifespan is one API process, so leaving one
and entering a fresh one over the same database is the restart. The Worker's
registration is fed straight into the app's registry, which is what the gRPC
session does on Register.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.dependencies import get_audit_recorder, get_current_user
from mc_server_dashboard_api.fleet.adapters.drain_store import (
    SqlAlchemyWorkerDrainStore,
)
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.domain.entities import WorkerStatus
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from tests.audit.fakes import RecordingAuditRecorder
from tests.fleet.fakes import FakeClock, make_worker
from tests.identity.fakes import make_user
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_T0 = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_T1 = _T0 + dt.timedelta(hours=1)
_WORKER_ID = str(uuid.uuid4())


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


@pytest.fixture
def control_plane_env(monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine) -> None:
    assert _DB_URL is not None
    monkeypatch.setenv("MCD_API_DATABASE__URL", _DB_URL)
    monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "true")
    monkeypatch.setenv("MCD_API_CONTROL__WORKER_CREDENTIAL", "shared-worker-secret")
    monkeypatch.setenv("MCD_API_CONTROL__TLS__INSECURE", "true")
    monkeypatch.setenv("MCD_API_SERVER__GRPC_PORT", "0")
    monkeypatch.setenv("MCD_API_SERVER__HOST", "127.0.0.1")


@contextmanager
def _api_process() -> Iterator[tuple[TestClient, InMemoryWorkerRegistry]]:
    """One API process: a fresh app whose lifespan runs for the block."""

    app = create_app()
    admin = make_user(is_platform_admin=True)
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[get_audit_recorder] = RecordingAuditRecorder
    with TestClient(app) as client:
        registry = app.state.worker_registry
        # What the gRPC session does on Register, with a fresh heartbeat so the
        # Worker is live under the app's real clock.
        registry.register(
            make_worker(worker_id=_WORKER_ID, at=dt.datetime.now(dt.timezone.utc))
        )
        yield client, registry


def _status(registry: InMemoryWorkerRegistry) -> WorkerStatus:
    snapshot = registry.get(WorkerId(_WORKER_ID))
    assert snapshot is not None
    return snapshot.status


@pytest.mark.usefixtures("control_plane_env")
def test_drain_survives_an_api_restart() -> None:
    with _api_process() as (client, _):
        resp = client.put(f"/api/workers/{_WORKER_ID}/drain")
        assert resp.status_code == 200
        assert resp.json() == {"servers_stopped": 0}

    with _api_process() as (client, registry):
        assert _status(registry) is WorkerStatus.DRAINING
        assert registry.candidates_for_placement() == []
        listed = client.get("/api/workers").json()["workers"]
        assert [w["status"] for w in listed] == ["draining"]


@pytest.mark.usefixtures("control_plane_env")
def test_undrain_survives_an_api_restart() -> None:
    with _api_process() as (client, _):
        assert client.put(f"/api/workers/{_WORKER_ID}/drain").status_code == 200

    with _api_process() as (client, registry):
        assert _status(registry) is WorkerStatus.DRAINING
        assert client.delete(f"/api/workers/{_WORKER_ID}/drain").status_code == 204

    with _api_process() as (_, registry):
        assert _status(registry) is WorkerStatus.ONLINE
        assert [c.worker_id for c in registry.candidates_for_placement()] == [
            WorkerId(_WORKER_ID)
        ]


@pytest.mark.usefixtures("control_plane_env")
def test_repeated_put_and_delete_are_idempotent() -> None:
    with _api_process() as (client, _):
        assert client.put(f"/api/workers/{_WORKER_ID}/drain").status_code == 200
        assert client.put(f"/api/workers/{_WORKER_ID}/drain").status_code == 200

    with _api_process() as (client, registry):
        assert _status(registry) is WorkerStatus.DRAINING
        assert client.delete(f"/api/workers/{_WORKER_ID}/drain").status_code == 204
        assert client.delete(f"/api/workers/{_WORKER_ID}/drain").status_code == 204

    with _api_process() as (_, registry):
        assert _status(registry) is WorkerStatus.ONLINE


def _store(engine: AsyncEngine, clock: FakeClock) -> SqlAlchemyWorkerDrainStore:
    return SqlAlchemyWorkerDrainStore(create_session_factory(engine), clock=clock)


async def test_store_redrain_keeps_one_row_and_the_first_drained_at(
    engine: AsyncEngine,
) -> None:
    clock = FakeClock(_T0)
    store = _store(engine, clock)
    worker_id = WorkerId(_WORKER_ID)

    await store.drain(worker_id)
    clock.set(_T1)
    await store.drain(worker_id)

    assert await store.drained() == frozenset({worker_id})
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT worker_id, drained_at FROM worker_drain"))
        ).all()
    assert rows == [(uuid.UUID(_WORKER_ID), _T0)]


async def test_store_undrain_of_an_undrained_worker_is_a_no_op(
    engine: AsyncEngine,
) -> None:
    clock = FakeClock(_T0)
    store = _store(engine, clock)
    drained = WorkerId(str(uuid.uuid4()))
    await store.drain(drained)

    await store.undrain(WorkerId(_WORKER_ID))

    assert await store.drained() == frozenset({drained})
