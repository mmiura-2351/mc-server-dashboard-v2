"""Plugin-cache GC lifespan gating (issue #1403).

The GC task must start whenever a plugin cache store exists, regardless of
``control.enabled``.  Before the fix it was nested under the control-plane
block, so ``control.enabled=false`` silently skipped it — leaking blobs.
"""

from __future__ import annotations

import asyncio

import pytest

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.servers.application.plugin_cache_gc import (
    RunPluginCacheGc,
)


async def test_plugin_cache_gc_starts_when_store_exists_and_control_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GC loop must be scheduled when plugin_cache_store is not None,
    even with control.enabled=false (the conftest default)."""

    # A sentinel that satisfies the ``plugin_cache_store is not None`` gate.
    sentinel_store = object()

    monkeypatch.setattr(
        "mc_server_dashboard_api.app._build_plugin_cache_store",
        lambda _settings: sentinel_store,
    )

    # Track whether run_plugin_cache_gc_loop was called, then return
    # immediately so the lifespan can proceed to yield.
    gc_loop_called = asyncio.Event()

    async def _fake_gc_loop(gc: RunPluginCacheGc, *, tick_seconds: float) -> None:
        gc_loop_called.set()

    monkeypatch.setattr(
        "mc_server_dashboard_api.app.run_plugin_cache_gc_loop",
        _fake_gc_loop,
    )

    app = create_app()
    async with app.router.lifespan_context(app):
        # The task is scheduled via create_task; yield to the event loop once
        # so the fake coroutine can execute.
        await asyncio.sleep(0)
        assert gc_loop_called.is_set(), (
            "plugin-cache GC loop was not started; "
            "it should run whenever a cache store exists"
        )
