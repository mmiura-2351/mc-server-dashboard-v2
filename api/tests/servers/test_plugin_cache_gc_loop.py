"""Tests for the plugin-cache GC lifespan loop driver (issue #1332).

The loop runs the GC use case on its cadence, survives a failing pass, and stops
cleanly when its task is cancelled (the shutdown path). The use case is replaced
with a tiny spy so these stay fast and deterministic.
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from mc_server_dashboard_api.servers.adapters.plugin_cache_gc_loop import (
    run_plugin_cache_gc_loop,
)
from mc_server_dashboard_api.servers.application.plugin_cache_gc import (
    PluginCacheGcResult,
    RunPluginCacheGc,
)


class _SpyGc:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.runs = 0
        self._fail_first = fail_first

    async def __call__(self) -> PluginCacheGcResult:
        self.runs += 1
        if self._fail_first and self.runs == 1:
            raise RuntimeError("boom")
        return PluginCacheGcResult(scanned=0, deleted=0, freed_bytes=0)


async def _run_for_runs(spy: _SpyGc, *, until: int) -> None:
    task = asyncio.create_task(
        run_plugin_cache_gc_loop(cast(RunPluginCacheGc, spy), tick_seconds=0)
    )
    for _ in range(10000):
        if spy.runs >= until:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_loop_sleeps_before_first_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop awaits a full cadence before its first tick (issue #1760)."""
    spy = _SpyGc()
    runs_at_first_sleep: list[int] = []
    real_sleep = asyncio.sleep

    async def _recording_sleep(delay: float) -> None:
        if not runs_at_first_sleep:
            runs_at_first_sleep.append(spy.runs)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _recording_sleep)
    task = asyncio.create_task(
        run_plugin_cache_gc_loop(cast(RunPluginCacheGc, spy), tick_seconds=1)
    )
    for _ in range(10000):
        if runs_at_first_sleep:
            break
        await real_sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runs_at_first_sleep == [0]


async def test_loop_runs_repeatedly() -> None:
    spy = _SpyGc()
    await _run_for_runs(spy, until=3)
    assert spy.runs >= 3


async def test_loop_survives_a_failing_pass() -> None:
    spy = _SpyGc(fail_first=True)
    await _run_for_runs(spy, until=3)
    assert spy.runs >= 3


async def test_loop_stops_cleanly_on_cancel() -> None:
    spy = _SpyGc()
    task = asyncio.create_task(
        run_plugin_cache_gc_loop(cast(RunPluginCacheGc, spy), tick_seconds=0)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
