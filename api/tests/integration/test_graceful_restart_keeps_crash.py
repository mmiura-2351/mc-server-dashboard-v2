"""A graceful API restart keeps a crashed server crashed (issue #3885).

Shutting the API down ends every Worker session, and each session's teardown
runs the disconnect's bulk observed-state write, which the shutdown waits for
(``WorkerSessionServicer.drain_teardowns``). These drive the real app against a
real database: each ``create_app`` lifespan is one API process, the Worker is a
real control-plane session still open when that process shuts down, and a fresh
lifespan over the same database is the restart.

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
from sqlalchemy.ext.asyncio import create_async_engine

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from mcsd.controlplane.v1.control_plane_pb2_grpc import WorkerServiceStub
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_CREDENTIAL = "shared-worker-secret"


@pytest.fixture
async def database() -> AsyncIterator[None]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    try:
        yield
    finally:
        await downgrade_base(_DB_URL)


@pytest.fixture
def control_plane_env(monkeypatch: pytest.MonkeyPatch, database: None) -> None:
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
def _api_process() -> Iterator[tuple[TestClient, InMemoryWorkerRegistry]]:
    """One API process: a fresh app whose lifespan runs for the block."""

    app = create_app()
    with TestClient(app) as client:
        yield client, app.state.worker_registry


async def _worker_session(port: int, worker_id: str) -> None:
    """A Worker's control stream: register as ``worker_id`` and stay connected.

    It ends when the API closes the stream, which is how a Worker meets an API
    shutdown.
    """

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
        with contextlib.suppress(aio.AioRpcError):
            while await call.read() is not aio.EOF:
                pass


def _connect_worker(
    client: TestClient, registry: InMemoryWorkerRegistry, port: int, worker_id: str
) -> None:
    """Open a Worker session on the app's loop, left open for the app's lifetime.

    Returns once the registration has rebuilt the Worker's load from its two
    assigned servers.
    """

    portal = client.portal
    assert portal is not None
    portal.start_task_soon(_worker_session, port, worker_id)
    _wait_for(lambda: [w.assigned_count for w in registry.list_workers()] == [2])


def _wait_for(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.02)


async def _seed(worker_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """A crash the Worker does not retry, and a running server, on one Worker."""

    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL)
    community_id, crashed, running = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
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
                    "config, slug, desired_state, observed_state, observed_at, "
                    "assigned_worker_id, created_at, updated_at, crash_reason, "
                    "crash_detail) VALUES "
                    "(:id, :cid, 'modded', 'java', '1.21', 'forge', '{}'::jsonb, "
                    "'k7m2p9', 'running', 'crashed', now(), :wid, "
                    "now() - interval '1 minute', now() - interval '1 minute', "
                    "'forge_install_out_of_memory', 'installer ran out of memory')"
                ),
                {"id": crashed, "cid": community_id, "wid": worker_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO server "
                    "(id, community_id, name, mc_edition, mc_version, server_type, "
                    "config, slug, desired_state, observed_state, observed_at, "
                    "assigned_worker_id, created_at, updated_at) VALUES "
                    "(:id, :cid, 'survival', 'java', '1.21', 'vanilla', "
                    "'{}'::jsonb, 'q3x8v4', 'running', 'running', now(), :wid, "
                    "now(), now())"
                ),
                {"id": running, "cid": community_id, "wid": worker_id},
            )
    finally:
        await engine.dispose()
    return crashed, running


async def _observed(
    server_id: uuid.UUID,
) -> tuple[str, dt.datetime | None, dt.datetime, str | None, str | None]:
    """The observed state and its stamp, ``updated_at`` and the stored crash."""

    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT observed_state, observed_at, updated_at, "
                        "crash_reason, crash_detail FROM server WHERE id = :id"
                    ),
                    {"id": server_id},
                )
            ).one()
    finally:
        await engine.dispose()
    return row[0], row[1], row[2], row[3], row[4]


@pytest.mark.usefixtures("control_plane_env")
def test_graceful_api_restart_keeps_a_crashed_server_crashed(
    grpc_ports: list[int],
) -> None:
    worker = uuid.uuid4()
    crashed, running = asyncio.run(_seed(worker))
    crash = asyncio.run(_observed(crashed))
    # Crashed after the row's last write, so the reconciler holds it
    # (``Server.crash_not_restarted``): both stamps must survive unchanged.
    assert crash[0] == "crashed"
    assert crash[1] is not None and crash[1] > crash[2]

    with _api_process() as (client, registry):
        _connect_worker(client, registry, grpc_ports[-1], str(worker))
    # The process shut down with the session open, and its teardown ran: the
    # running server is now unknown.
    assert asyncio.run(_observed(running))[0] == "unknown"
    assert asyncio.run(_observed(crashed)) == crash

    # The restart: the startup reset runs, and the Worker reconnects reporting
    # nothing for the instance it has forgotten.
    with _api_process() as (client, registry):
        _connect_worker(client, registry, grpc_ports[-1], str(worker))
        assert asyncio.run(_observed(crashed)) == crash
    assert asyncio.run(_observed(crashed)) == crash
