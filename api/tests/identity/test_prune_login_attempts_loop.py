"""Tests for the login_attempt prune lifespan loop driver (SECURITY.md 3).

The loop ticks on its cadence, survives a failing tick, and stops cleanly when
its task is cancelled (the shutdown path). The pruner is replaced with a tiny spy
so these stay fast and deterministic.
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from mc_server_dashboard_api.identity.adapters.prune_login_attempts_loop import (
    run_prune_login_attempts_loop,
)
from mc_server_dashboard_api.identity.application.prune_login_attempts import (
    PruneLoginAttempts,
)


class _SpyPruner:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.ticks = 0
        self._fail_first = fail_first

    async def tick(self) -> None:
        self.ticks += 1
        if self._fail_first and self.ticks == 1:
            raise RuntimeError("boom")


async def _run_for_ticks(spy: _SpyPruner, *, until: int) -> None:
    task = asyncio.create_task(
        run_prune_login_attempts_loop(cast(PruneLoginAttempts, spy), tick_seconds=0)
    )
    for _ in range(10000):
        if spy.ticks >= until:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_loop_ticks_repeatedly() -> None:
    spy = _SpyPruner()
    await _run_for_ticks(spy, until=3)
    assert spy.ticks >= 3


async def test_loop_survives_a_failing_tick() -> None:
    spy = _SpyPruner(fail_first=True)
    await _run_for_ticks(spy, until=3)
    # The first tick raised; the loop kept going and ticked again.
    assert spy.ticks >= 3


async def test_loop_stops_cleanly_on_cancel() -> None:
    spy = _SpyPruner()
    task = asyncio.create_task(
        run_prune_login_attempts_loop(cast(PruneLoginAttempts, spy), tick_seconds=0)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
