"""An API start gives each Worker a full grace to re-register (issue #3916).

The reconciler's first tick runs as the API starts, before any Worker has
registered with the new process. These drive the real app against a real
database, so the instant the lifespan hands the reconciler is the one under test:
a stopped-intent row whose own grace lapsed while no API ran is still assigned
after that tick.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.core.adapters.metrics import (
    reconciler_last_success_timestamp_seconds,
)
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)


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
    monkeypatch.setenv("MCD_API_CONTROL__WORKER_CREDENTIAL", "shared-worker-secret")
    monkeypatch.setenv("MCD_API_CONTROL__TLS__INSECURE", "true")
    monkeypatch.setenv("MCD_API_SERVER__GRPC_PORT", "0")
    monkeypatch.setenv("MCD_API_SERVER__HOST", "127.0.0.1")


async def _seed(worker_id: uuid.UUID) -> list[uuid.UUID]:
    """One row per observed state that reaches the first tick with its old stamp.

    Each is under a stopped intent, still assigned to the Worker, and last
    written an hour ago, well past the stock 660 s grace. The start-up reset
    leaves these states alone.
    """

    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL)
    community_id = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(3)]
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO community (id, name, created_at, updated_at) "
                    "VALUES (:id, 'guild', now(), now())"
                ),
                {"id": community_id},
            )
            for server_id, observed, slug in zip(
                ids, ("stopped", "crashed", "unknown"), ("b4t7k2", "c9n3w5", "d2q8m6")
            ):
                await conn.execute(
                    text(
                        "INSERT INTO server "
                        "(id, community_id, name, mc_edition, mc_version, "
                        "server_type, config, slug, desired_state, observed_state, "
                        "observed_at, assigned_worker_id, created_at, updated_at) "
                        "VALUES (:id, :cid, :name, 'java', '1.21', 'vanilla', "
                        "'{}'::jsonb, :slug, 'stopped', :observed, "
                        "now() - interval '1 hour', :wid, now() - interval '1 hour', "
                        "now() - interval '1 hour')"
                    ),
                    {
                        "id": server_id,
                        "cid": community_id,
                        "name": observed,
                        "slug": slug,
                        "observed": observed,
                        "wid": worker_id,
                    },
                )
    finally:
        await engine.dispose()
    return ids


async def _assigned(server_ids: list[uuid.UUID]) -> list[uuid.UUID | None]:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            return [
                (
                    await conn.execute(
                        text("SELECT assigned_worker_id FROM server WHERE id = :id"),
                        {"id": server_id},
                    )
                ).scalar_one()
                for server_id in server_ids
            ]
    finally:
        await engine.dispose()


def _wait_for(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 30
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.02)


@pytest.mark.usefixtures("control_plane_env")
def test_the_boot_tick_keeps_the_rows_of_a_worker_not_back_yet() -> None:
    worker = uuid.uuid4()
    server_ids = asyncio.run(_seed(worker))
    last_tick = reconciler_last_success_timestamp_seconds._value.get()

    with TestClient(create_app()):
        # Stamped only once a tick has completed.
        _wait_for(
            lambda: reconciler_last_success_timestamp_seconds._value.get() != last_tick
        )
        assert asyncio.run(_assigned(server_ids)) == [worker] * 3
