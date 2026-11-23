"""The schedule loop's shutdown drain (issue #4109).

On shutdown the loop takes no further tick, the tick in flight gets up to the
drain to finish, and only a tick still running after it is cancelled. Driven
with a fake runner and real waits of a few hundredths of a second.
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast

import pytest

from mc_server_dashboard_api.servers.adapters import schedule_loop
from mc_server_dashboard_api.servers.adapters.schedule_loop import (
    drain_schedule_loop,
    run_schedule_loop,
)
from mc_server_dashboard_api.servers.application.schedule_runner import RunScheduleTick

# Far longer than any test here takes, so a wait on it never ends one.
_LONG = 3600.0


class _Runner:
    """Counts its ticks; with ``hold`` set, each tick waits for ``release``."""

    def __init__(self, *, hold: bool = False) -> None:
        self._hold = hold
        self.started = 0
        self.finished = 0
        self.cancelled = 0
        self.in_tick = asyncio.Event()
        self.release = asyncio.Event()

    async def tick(self) -> None:
        self.started += 1
        self.in_tick.set()
        try:
            if self._hold:
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        self.finished += 1


def _start(
    runner: _Runner, *, tick_seconds: float
) -> tuple[asyncio.Task[None], asyncio.Event]:
    stopping = asyncio.Event()
    loop = asyncio.create_task(
        run_schedule_loop(
            cast(RunScheduleTick, runner), tick_seconds=tick_seconds, stopping=stopping
        )
    )
    return loop, stopping


def _drain_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == schedule_loop.__name__
    ]


async def test_a_loop_stopped_between_ticks_takes_no_further_tick() -> None:
    runner = _Runner()
    loop, stopping = _start(runner, tick_seconds=_LONG)

    async with asyncio.timeout(5):
        await drain_schedule_loop(loop, stopping, drain_seconds=_LONG)

    assert loop.done() and not loop.cancelled()
    assert runner.started == 0


async def test_a_loop_stopped_mid_tick_takes_no_tick_after_it() -> None:
    runner = _Runner(hold=True)
    loop, stopping = _start(runner, tick_seconds=0.01)
    await runner.in_tick.wait()

    drain = asyncio.create_task(
        drain_schedule_loop(loop, stopping, drain_seconds=_LONG)
    )
    # Several cadences pass while the tick is held.
    await asyncio.sleep(0.05)
    runner.release.set()
    async with asyncio.timeout(5):
        await drain
    await asyncio.sleep(0.05)

    assert (runner.started, runner.finished) == (1, 1)


async def test_a_drain_lets_the_tick_in_flight_finish(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    runner = _Runner(hold=True)
    loop, stopping = _start(runner, tick_seconds=0.01)
    await runner.in_tick.wait()

    drain = asyncio.create_task(
        drain_schedule_loop(loop, stopping, drain_seconds=_LONG)
    )
    await asyncio.sleep(0.05)
    assert not drain.done()
    runner.release.set()
    async with asyncio.timeout(5):
        await drain

    assert (runner.finished, runner.cancelled) == (1, 0)
    assert [level for level, _ in _drain_lines(caplog)] == [logging.INFO] * 2


async def test_a_drain_that_runs_out_cancels_the_tick_in_flight(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    runner = _Runner(hold=True)
    loop, stopping = _start(runner, tick_seconds=0.01)
    await runner.in_tick.wait()

    async with asyncio.timeout(5):
        await drain_schedule_loop(loop, stopping, drain_seconds=0.05)

    assert loop.cancelled()
    assert (runner.finished, runner.cancelled) == (0, 1)
    assert [level for level, _ in _drain_lines(caplog)] == [
        logging.INFO,
        logging.WARNING,
    ]
