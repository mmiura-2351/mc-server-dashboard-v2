"""Tests for the reconciler lifespan loop driver (issue #101, #230).

The loop ticks on its cadence, survives a failing tick, and stops cleanly when
its task is cancelled (the shutdown path). It also runs the startup
observed-state reset as its first action and gates ticking on that reset
succeeding (issue #230): a reset that fails is retried on the next iteration and
the tick is skipped until it succeeds, so a briefly-unreachable DB at startup
never crashes the process and the reconciler never acts on the stale cache. The
reconciler and reset are replaced with tiny spies so these stay fast and
deterministic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast

import pytest

from mc_server_dashboard_api.servers.adapters.reconciler_loop import (
    run_reconciler_loop,
)
from mc_server_dashboard_api.servers.application.reconciler import RunReconcilerTick
from mc_server_dashboard_api.servers.application.startup_reset import (
    ResetUnverifiableObservedStates,
)


class _SpyReconciler:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.ticks = 0
        self._fail_first = fail_first

    async def tick(self) -> None:
        self.ticks += 1
        if self._fail_first and self.ticks == 1:
            raise RuntimeError("boom")


class _SpyReset:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls = 0
        self._fail_times = fail_times

    async def __call__(self) -> int:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("db down")
        return 0


def _start_loop(spy: _SpyReconciler, reset: _SpyReset) -> asyncio.Task[None]:
    return asyncio.create_task(
        run_reconciler_loop(
            cast(RunReconcilerTick, spy),
            reset=cast(ResetUnverifiableObservedStates, reset),
            tick_seconds=0,
        )
    )


async def _run_until(
    predicate: Callable[[], bool], *, task: asyncio.Task[None]
) -> None:
    for _ in range(10000):
        if predicate():
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _run_for_ticks(spy: _SpyReconciler, reset: _SpyReset, *, until: int) -> None:
    task = _start_loop(spy, reset)
    await _run_until(lambda: spy.ticks >= until, task=task)


async def test_loop_ticks_repeatedly() -> None:
    spy = _SpyReconciler()
    await _run_for_ticks(spy, _SpyReset(), until=3)
    assert spy.ticks >= 3


async def test_loop_survives_a_failing_tick() -> None:
    spy = _SpyReconciler(fail_first=True)
    await _run_for_ticks(spy, _SpyReset(), until=3)
    assert spy.ticks >= 3


async def test_loop_stops_cleanly_on_cancel() -> None:
    spy = _SpyReconciler()
    task = _start_loop(spy, _SpyReset())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_reset_runs_before_the_first_tick() -> None:
    spy = _SpyReconciler()
    reset = _SpyReset()
    task = _start_loop(spy, reset)
    # As soon as a tick happens the reset must already have run.
    await _run_until(lambda: spy.ticks >= 1, task=task)
    assert reset.calls >= 1
    assert spy.ticks >= 1


async def test_tick_skipped_until_reset_succeeds_then_resumes() -> None:
    spy = _SpyReconciler()
    reset = _SpyReset(fail_times=2)
    task = _start_loop(spy, reset)
    # The reset fails the first two attempts; no tick may run while it fails.
    await _run_until(lambda: reset.calls >= 2, task=task)
    # Reset has been attempted but not yet succeeded -> tick was skipped.
    assert spy.ticks == 0

    spy2 = _SpyReconciler()
    reset2 = _SpyReset(fail_times=2)
    task2 = _start_loop(spy2, reset2)
    # Once the reset succeeds (third call) the tick resumes.
    await _run_until(lambda: spy2.ticks >= 1, task=task2)
    assert reset2.calls >= 3
    assert spy2.ticks >= 1


async def test_reset_runs_exactly_once_after_success() -> None:
    spy = _SpyReconciler()
    reset = _SpyReset()
    task = _start_loop(spy, reset)
    await _run_until(lambda: spy.ticks >= 5, task=task)
    # The reset succeeded on its first call and is never invoked again.
    assert reset.calls == 1
    assert spy.ticks >= 5
