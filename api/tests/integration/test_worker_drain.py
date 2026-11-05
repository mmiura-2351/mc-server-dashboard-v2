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

import asyncio
import contextlib
import datetime as dt
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from grpc import aio
from grpc.aio._server import Server as ConcreteAioServer
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
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from mcsd.controlplane.v1.control_plane_pb2_grpc import WorkerServiceStub
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
_CREDENTIAL = "shared-worker-secret"


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
    monkeypatch.setenv("MCD_API_CONTROL__WORKER_CREDENTIAL", _CREDENTIAL)
    monkeypatch.setenv("MCD_API_CONTROL__TLS__INSECURE", "true")
    monkeypatch.setenv("MCD_API_SERVER__GRPC_PORT", "0")
    monkeypatch.setenv("MCD_API_SERVER__HOST", "127.0.0.1")


@pytest.fixture
def grpc_ports(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The port each API process's control-plane listener bound, in order."""

    ports: list[int] = []
    real_add = ConcreteAioServer.add_insecure_port

    def _spy(self: aio.Server, address: str) -> int:
        port = int(real_add(self, address))
        ports.append(port)
        return port

    monkeypatch.setattr(ConcreteAioServer, "add_insecure_port", _spy)
    return ports


@contextmanager
def _api_app() -> Iterator[tuple[TestClient, InMemoryWorkerRegistry]]:
    """One API process with no Worker yet: a fresh app whose lifespan runs."""

    app = create_app()
    admin = make_user(is_platform_admin=True)
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[get_audit_recorder] = RecordingAuditRecorder
    with TestClient(app) as client:
        yield client, app.state.worker_registry


async def _worker_session(port: int, worker_id: str) -> None:
    """A Worker's control stream: register as ``worker_id`` and stay connected."""

    async with aio.insecure_channel(f"127.0.0.1:{port}") as channel:
        call = WorkerServiceStub(channel).Session(
            metadata=[("authorization", f"Bearer {_CREDENTIAL}")]
        )
        await call.write(
            pb.WorkerMessage(
                correlation_id="reg-1",
                register=pb.Register(
                    worker_id=worker_id,
                    worker_version="1.0.0",
                    capabilities=pb.WorkerCapabilities(
                        drivers=[pb.EXECUTION_DRIVER_KIND_CONTAINER], max_servers=4
                    ),
                ),
            )
        )
        # Ignore whatever the API sends; the session lives until cancelled.
        while await call.read() is not aio.EOF:
            pass


@contextmanager
def _connected_worker(client: TestClient, port: int, worker_id: str) -> Iterator[None]:
    """Hold a Worker session on the app's own event loop for the block."""

    portal = client.portal
    assert portal is not None
    session = portal.start_task_soon(_worker_session, port, worker_id)
    try:
        yield
    finally:
        session.cancel()
        with contextlib.suppress(BaseException):
            session.result(timeout=5)


def _wait_for(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.02)


async def _seed_running_server(worker_id: uuid.UUID) -> uuid.UUID:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL)
    community_id, server_id = uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO community (id, name, created_at, updated_at) "
                    "VALUES (:id, 'guild', now(), now())"
                ),
                {"id": community_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO server "
                    "(id, community_id, name, mc_edition, mc_version, server_type, "
                    "config, slug, desired_state, observed_state, "
                    "assigned_worker_id, created_at, updated_at) VALUES "
                    "(:id, :cid, 'survival', 'java', '1.21', 'vanilla', "
                    "'{}'::jsonb, 'k7m2p9', 'running', 'running', :wid, "
                    "now(), now())"
                ),
                {"id": server_id, "cid": community_id, "wid": worker_id},
            )
    finally:
        await engine.dispose()
    return server_id


async def _server_row(server_id: uuid.UUID) -> tuple[str, uuid.UUID | None]:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT desired_state, assigned_worker_id FROM server "
                        "WHERE id = :id"
                    ),
                    {"id": server_id},
                )
            ).one()
    finally:
        await engine.dispose()
    return row[0], row[1]


@pytest.mark.usefixtures("control_plane_env")
def test_drain_of_a_non_canonical_worker_id_survives_a_restart(
    grpc_ports: list[int],
) -> None:
    # Issue #3535 x #3542: a Worker configured with an uppercase UUID registers,
    # is drained through that spelling, and after an API restart reconnects with
    # the lowercase spelling. The drain row, the restored set, the registry, and
    # the server assignment all key on the canonical id, so it is one Worker and
    # it comes back draining.
    canonical = str(uuid.uuid4())
    upper = canonical.upper()
    server_id = asyncio.run(_seed_running_server(uuid.UUID(canonical)))

    with _api_app() as (client, registry):
        with _connected_worker(client, grpc_ports[-1], upper):
            # The registration rebuilt its load from the canonical assignment.
            _wait_for(
                lambda: (
                    [(w.id, w.assigned_count) for w in registry.list_workers()]
                    == [(WorkerId(canonical), 1)]
                )
            )
            resp = client.put(f"/api/workers/{upper}/drain")
            assert resp.status_code == 200
            assert resp.json() == {"servers_stopped": 1}

    assert asyncio.run(_server_row(server_id)) == ("stopped", uuid.UUID(canonical))

    with _api_app() as (client, registry):
        with _connected_worker(client, grpc_ports[-1], canonical):
            _wait_for(lambda: len(registry.list_workers()) == 1)
            [worker] = registry.list_workers()
            assert worker.id == WorkerId(canonical)
            assert worker.status is WorkerStatus.DRAINING
            assert worker.assigned_count == 0
            assert registry.candidates_for_placement() == []
            listed = client.get("/api/workers").json()["workers"]
            assert [(w["id"], w["status"]) for w in listed] == [(canonical, "draining")]


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
