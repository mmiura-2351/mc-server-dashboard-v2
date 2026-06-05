"""Tests for the JAR-pool GC lifespan loop driver (D4, issue #293).

The loop runs the GC use case on its cadence, survives a failing pass, and stops
cleanly when its task is cancelled (the shutdown path). The use case is replaced
with a tiny spy so these stay fast and deterministic. Mirrors the snapshot/backup
loop tests.
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from mc_server_dashboard_api.versions.adapters.jar_gc_loop import run_jar_gc_loop
from mc_server_dashboard_api.versions.application.jar_gc import (
    JarGcResult,
    RunJarPoolGc,
)


class _SpyGc:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.runs = 0
        self._fail_first = fail_first

    async def __call__(self) -> JarGcResult:
        self.runs += 1
        if self._fail_first and self.runs == 1:
            raise RuntimeError("boom")
        return JarGcResult(scanned=0, deleted=0, freed_bytes=0)


async def _run_for_runs(spy: _SpyGc, *, until: int) -> None:
    task = asyncio.create_task(run_jar_gc_loop(cast(RunJarPoolGc, spy), tick_seconds=0))
    for _ in range(10000):
        if spy.runs >= until:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


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
    task = asyncio.create_task(run_jar_gc_loop(cast(RunJarPoolGc, spy), tick_seconds=0))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
