"""Resource-pack GC lifespan wiring (issue #3277).

The GC task starts whenever a resource pack store exists, regardless of
``control.enabled`` -- uploads and deletes work without the control plane, so the
orphans they leave do too -- and it runs on the storage sweep's cadence.
"""

from __future__ import annotations

import asyncio

import pytest

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.servers.application.resource_pack_gc import (
    RunResourcePackGc,
)


async def test_resource_pack_gc_starts_when_store_exists_and_control_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A sentinel that satisfies the ``resource_pack_store is not None`` gate.
    sentinel_store = object()
    monkeypatch.setattr(
        "mc_server_dashboard_api.app._build_resource_pack_store",
        lambda _settings: sentinel_store,
    )
    monkeypatch.setenv("MCD_API_STORAGE_SWEEP__INTERVAL_SECONDS", "4321")
    started: list[tuple[RunResourcePackGc, float]] = []

    async def _fake_gc_loop(gc: RunResourcePackGc, *, tick_seconds: float) -> None:
        started.append((gc, tick_seconds))

    monkeypatch.setattr(
        "mc_server_dashboard_api.app.run_resource_pack_gc_loop", _fake_gc_loop
    )

    app = create_app()
    async with app.router.lifespan_context(app):
        # The task is scheduled via create_task; yield to the event loop once so
        # the fake coroutine can execute.
        await asyncio.sleep(0)
        ((gc, tick_seconds),) = started
        assert gc.store is sentinel_store
        assert tick_seconds == 4321
