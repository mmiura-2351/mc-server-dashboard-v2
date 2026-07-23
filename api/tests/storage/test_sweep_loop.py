"""Tests for the storage crash-recovery sweep lifespan loop (issue #2252).

The loop runs ``storage.sweep`` on its cadence, survives a failing pass, and
stops cleanly when its task is cancelled (the shutdown path). It must work for
both the async object adapter (``sweep`` is a coroutine function) and the
blocking fs adapter (``sweep`` is sync, run off the event loop), so every case
is parametrized over both variants. The sweep is replaced with a tiny spy so
these stay fast and deterministic. Mirrors ``tests/versions/test_jar_gc_loop``.
"""

from __future__ import annotations

import asyncio

import pytest

from mc_server_dashboard_api.storage.adapters.sweep_loop import (
    run_storage_sweep_loop,
)


class _SpySweep:
    """A sweep callable exposing either a sync or an async ``sweep`` method.

    The async variant is a bound coroutine method (``iscoroutinefunction`` True,
    like the object adapter); the sync variant a plain method (False, like the
    fs adapter), so the loop's sync/async dispatch is exercised for real.
    """

    def __init__(self, *, is_async: bool, fail_first: bool = False) -> None:
        self.runs = 0
        self._fail_first = fail_first
        self.sweep = self._async_sweep if is_async else self._sync_sweep

    def _tick(self) -> None:
        self.runs += 1
        if self._fail_first and self.runs == 1:
            raise RuntimeError("boom")

    async def _async_sweep(self) -> None:
        self._tick()

    def _sync_sweep(self) -> None:
        self._tick()


async def _run_for_runs(spy: _SpySweep, *, until: int) -> None:
    task = asyncio.create_task(run_storage_sweep_loop(spy.sweep, tick_seconds=0))
    for _ in range(10000):
        if spy.runs >= until:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("is_async", [True, False])
async def test_loop_sleeps_before_first_tick(
    monkeypatch: pytest.MonkeyPatch, is_async: bool
) -> None:
    """The loop awaits a full cadence before its first tick (issue #1760)."""
    spy = _SpySweep(is_async=is_async)
    runs_at_first_sleep: list[int] = []
    real_sleep = asyncio.sleep

    async def _recording_sleep(delay: float) -> None:
        if not runs_at_first_sleep:
            runs_at_first_sleep.append(spy.runs)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _recording_sleep)
    task = asyncio.create_task(run_storage_sweep_loop(spy.sweep, tick_seconds=1))
    for _ in range(10000):
        if runs_at_first_sleep:
            break
        await real_sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runs_at_first_sleep == [0]


@pytest.mark.parametrize("is_async", [True, False])
async def test_loop_runs_repeatedly(is_async: bool) -> None:
    spy = _SpySweep(is_async=is_async)
    await _run_for_runs(spy, until=3)
    assert spy.runs >= 3


@pytest.mark.parametrize("is_async", [True, False])
async def test_loop_survives_a_failing_pass(is_async: bool) -> None:
    spy = _SpySweep(is_async=is_async, fail_first=True)
    await _run_for_runs(spy, until=3)
    assert spy.runs >= 3


@pytest.mark.parametrize("is_async", [True, False])
async def test_loop_stops_cleanly_on_cancel(is_async: bool) -> None:
    spy = _SpySweep(is_async=is_async)
    task = asyncio.create_task(run_storage_sweep_loop(spy.sweep, tick_seconds=0))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
