"""Tests for the backup-scheduler lifespan loop driver (FR-BAK-3).

The loop ticks on its cadence, survives a failing tick, and stops cleanly when
its task is cancelled (the shutdown path). The scheduler is replaced with a tiny
spy so these stay fast and deterministic.
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from mc_server_dashboard_api.servers.adapters.backup_loop import run_backup_loop
from mc_server_dashboard_api.servers.application.backup_scheduler import (
    RunBackupScheduleTick,
)


class _SpyScheduler:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.ticks = 0
        self._fail_first = fail_first

    async def tick(self) -> None:
        self.ticks += 1
        if self._fail_first and self.ticks == 1:
            raise RuntimeError("boom")


async def _run_for_ticks(spy: _SpyScheduler, *, until: int) -> None:
    task = asyncio.create_task(
        run_backup_loop(cast(RunBackupScheduleTick, spy), tick_seconds=0)
    )
    for _ in range(10000):
        if spy.ticks >= until:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_loop_sleeps_before_first_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop awaits a full cadence before its first tick (issue #1760)."""
    spy = _SpyScheduler()
    ticks_at_first_sleep: list[int] = []
    real_sleep = asyncio.sleep

    async def _recording_sleep(delay: float) -> None:
        if not ticks_at_first_sleep:
            ticks_at_first_sleep.append(spy.ticks)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _recording_sleep)
    task = asyncio.create_task(
        run_backup_loop(cast(RunBackupScheduleTick, spy), tick_seconds=1)
    )
    for _ in range(10000):
        if ticks_at_first_sleep:
            break
        await real_sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ticks_at_first_sleep == [0]


async def test_loop_ticks_repeatedly() -> None:
    spy = _SpyScheduler()
    await _run_for_ticks(spy, until=3)
    assert spy.ticks >= 3


async def test_loop_survives_a_failing_tick() -> None:
    spy = _SpyScheduler(fail_first=True)
    await _run_for_ticks(spy, until=3)
    assert spy.ticks >= 3


async def test_loop_stops_cleanly_on_cancel() -> None:
    spy = _SpyScheduler()
    task = asyncio.create_task(
        run_backup_loop(cast(RunBackupScheduleTick, spy), tick_seconds=0)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
