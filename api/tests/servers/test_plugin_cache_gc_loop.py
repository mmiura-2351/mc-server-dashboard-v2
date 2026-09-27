"""Wiring test for the plugin-cache GC lifespan task."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import pytest

from mc_server_dashboard_api.servers.adapters import plugin_cache_gc_loop
from mc_server_dashboard_api.servers.application.plugin_cache_gc import (
    PluginCacheGcResult,
    RunPluginCacheGc,
)


class _SpyGc:
    def __init__(self) -> None:
        self.runs = 0

    async def __call__(self) -> PluginCacheGcResult:
        self.runs += 1
        return PluginCacheGcResult(scanned=0, deleted=0, freed_bytes=0)


async def test_wires_gc_and_interval_to_periodic_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gc = _SpyGc()
    runner = AsyncMock()
    monkeypatch.setattr(plugin_cache_gc_loop, "run_periodic", runner)

    await plugin_cache_gc_loop.run_plugin_cache_gc_loop(
        cast(RunPluginCacheGc, gc), tick_seconds=17
    )

    runner.assert_awaited_once()
    call = runner.await_args
    assert call is not None
    callback = call.args[0]
    assert call.kwargs["tick_seconds"] == 17
    assert callable(call.kwargs["on_error"])
    await callback()
    assert gc.runs == 1
