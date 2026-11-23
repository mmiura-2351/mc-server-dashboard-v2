"""An exhausted pool's wait for a connection against a real PostgreSQL (issue #4051).

Runs only when ``MCD_TEST_DATABASE_URL`` is set; skipped otherwise (TESTING.md
Section 5). Each engine here has one connection and no overflow, and the test
holds that connection, so the next checkout waits out ``pool_timeout``. That wait
fails as the other bounds do: as a transient database timeout, answered 503
``database_unavailable`` on REST, and holding a Worker's report through its
retries instead of dropping it.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import pytest
import sqlalchemy.exc
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.core.adapters.database import (
    create_engine,
    create_session_factory,
    is_database_timeout,
    is_transient_database_error,
)
from mc_server_dashboard_api.dependencies import _build_token_service
from mc_server_dashboard_api.fleet.adapters import grpc_server
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.identity.adapters.clock import SystemClock
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from tests.client_utils import enter_client
from tests.core.fakes import first_failure
from tests.fleet.fakes import FakeClock, FakeDatabasePing
from tests.fleet.test_grpc_session import (
    _GRPC_SERVER_LOGGER,
    _HOLD_APPLIED,
    _HOLD_REFUSED,
    _HOLD_STARTED,
    _T0,
    _TIMEOUT,
    _WORKER_ID,
    _Harness,
    _logged,
    _RealWriteSink,
    _registered,
    _status_message,
    _wait_until,
)
from tests.identity.fakes import make_user
from tests.integration.test_observed_state_status_events import (  # noqa: F401
    _World,
    world,
)
from tests.test_http_database_outage import _ROUTE, _UNAVAILABLE

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_POOL_TIMEOUT_SECONDS = 0.2


def _one_connection_engine() -> AsyncEngine:
    assert _DB_URL is not None
    return create_engine(
        _DB_URL, pool_size=1, max_overflow=0, pool_timeout=_POOL_TIMEOUT_SECONDS
    )


async def _checkout(engine: AsyncEngine) -> None:
    async with engine.connect():
        pass


async def test_a_wait_past_the_bound_fails_as_a_transient_database_timeout() -> None:
    engine = _one_connection_engine()
    try:
        async with engine.connect():
            error, elapsed = await first_failure(lambda: _checkout(engine))
    finally:
        await engine.dispose()

    assert elapsed < 2.0
    assert isinstance(error, sqlalchemy.exc.TimeoutError)
    assert is_transient_database_error(error) is True
    assert is_database_timeout(error) is True


# A stall guard: without the bound the request waits SQLAlchemy's 30 s.
@pytest.mark.timeout(10)
def test_a_wait_past_the_bound_answers_503(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real app, its engine swapped for the exhausted one: authenticating the
    # caller is the route's first checkout.
    monkeypatch.setenv(
        "MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@127.0.0.1:5/none"
    )
    client = enter_client(TestClient(create_app(), raise_server_exceptions=False))
    app = client.app
    assert isinstance(app, FastAPI) and client.portal is not None
    engine = _one_connection_engine()
    monkeypatch.setattr(app.state, "engine", engine)
    token = _build_token_service(
        app.state.settings.auth.token, SystemClock()
    ).issue_access_token(make_user().id)

    # On the app's loop, which the engine's connections are bound to.
    async def hold() -> AsyncConnection:
        return await engine.connect()

    held = client.portal.call(hold)
    try:
        resp = client.get(_ROUTE, headers={"Authorization": f"Bearer {token}"})
    finally:
        client.portal.call(held.close)
        client.portal.call(engine.dispose)

    assert (resp.status_code, resp.json()) == (503, _UNAVAILABLE)
    assert resp.headers["retry-after"] == "5"


class _CountingSink(_RealWriteSink):
    """The real sink's writes, counted."""

    def __init__(self, real: ServersServerStateSink) -> None:
        super().__init__(real)
        self.attempts = 0

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str, **frame: Any
    ) -> bool:
        self.attempts += 1
        return await super().record_observed_state(
            server_id=server_id, worker_id=worker_id, state=state, **frame
        )


async def test_a_report_whose_retries_wait_out_the_pool_stays_held(
    world: _World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Other work holding the pool is not the report's own failure, so the pings
    # answering both sides of a retry do not drop it: it stays held, and lands
    # once a connection is free.
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    monkeypatch.setattr(grpc_server, "_HOLD_RETRY_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(grpc_server, "_HOLD_RETRY_MAX_SECONDS", 0.02)
    server = await world.server("survival", worker=uuid.UUID(_WORKER_ID))
    engine = _one_connection_engine()
    sink = _CountingSink(
        ServersServerStateSink(
            create_session_factory(engine),
            clock=world.clock,
            real_time_events=world.bus,
        )
    )
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        world.fleet_clock(),
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=True),
    )
    held = await engine.connect()
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(str(server.id.value), pb.SERVER_STATE_RUNNING))
        await _wait_until(lambda: sink.attempts >= 3, "the report was not retried")
        await held.close()
        await _wait_until(
            lambda: _logged(caplog, _HOLD_APPLIED), "the report was not applied"
        )
        await call.done_writing()
    finally:
        await held.close()
        await h.stop()
        await engine.dispose()

    assert _logged(caplog, _HOLD_STARTED) != []
    assert _logged(caplog, _HOLD_REFUSED) == []
    assert (await world.observed(server)).value == "running"
