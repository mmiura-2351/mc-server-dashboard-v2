"""The lifespan shutdown drains the schedule loop (issue #4109).

These drive the real ``create_app`` lifespan with the control plane on, so the
schedule loop runs, over a runner whose tick the test holds open: the tick in
flight when the shutdown begins gets ``schedule.shutdown_drain_seconds`` to
finish, and is cancelled once that runs out. The loop's cadence is cut to a
hundredth of a second so the first tick starts at once.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.fleet.adapters.drain_store import (
    SqlAlchemyWorkerDrainStore,
)
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from mc_server_dashboard_api.servers.adapters.schedule_loop import run_schedule_loop

# Generous: an app startup on a contended host can take seconds, and only a
# hang should reach it.
_STARTUP_BOUND = 60


class _HeldRunner:
    """A runner whose tick waits for ``release``."""

    def __init__(self) -> None:
        self.in_tick = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = 0
        self.cancelled = 0

    async def tick(self) -> None:
        self.in_tick.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        self.finished += 1


async def _drained(self: SqlAlchemyWorkerDrainStore) -> frozenset[WorkerId]:
    return frozenset()


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _HeldRunner:
    monkeypatch.setenv("MCD_API_STORAGE__FS__ROOT", str(tmp_path))
    monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "true")
    monkeypatch.setenv("MCD_API_CONTROL__WORKER_CREDENTIAL", "shared-worker-secret")
    monkeypatch.setenv("MCD_API_SERVER__GRPC_PORT", "0")
    monkeypatch.setenv("MCD_API_SERVER__HOST", "127.0.0.1")
    monkeypatch.setattr(SqlAlchemyWorkerDrainStore, "drained", _drained)
    held = _HeldRunner()
    monkeypatch.setattr(
        "mc_server_dashboard_api.app.RunScheduleTick", lambda **_kwargs: held
    )

    def _fast_loop(target: Any, **kwargs: Any) -> Any:
        return run_schedule_loop(target, **{**kwargs, "tick_seconds": 0.01})

    monkeypatch.setattr("mc_server_dashboard_api.app.run_schedule_loop", _fast_loop)
    return held


async def _serve_until(leave: asyncio.Event, started: asyncio.Event) -> None:
    app = create_app()
    async with app.router.lifespan_context(app):
        started.set()
        await leave.wait()


async def _shut_down_mid_tick(runner: _HeldRunner) -> asyncio.Task[None]:
    """Start the app, wait for a tick to begin, then begin its shutdown."""

    leave, started = asyncio.Event(), asyncio.Event()
    serving = asyncio.create_task(_serve_until(leave, started))
    async with asyncio.timeout(_STARTUP_BOUND):
        await started.wait()
        await runner.in_tick.wait()
    leave.set()
    return serving


async def test_a_shutdown_lets_the_scheduled_tick_in_flight_finish(
    runner: _HeldRunner,
) -> None:
    serving = await _shut_down_mid_tick(runner)
    # The shutdown has begun, and the tick is still running.
    await asyncio.sleep(0.2)
    assert not serving.done()

    runner.release.set()
    async with asyncio.timeout(_STARTUP_BOUND):
        await serving

    assert (runner.finished, runner.cancelled) == (1, 0)


async def test_a_shutdown_cancels_the_scheduled_tick_once_its_drain_runs_out(
    runner: _HeldRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_SCHEDULE__SHUTDOWN_DRAIN_SECONDS", "0")

    serving = await _shut_down_mid_tick(runner)
    async with asyncio.timeout(_STARTUP_BOUND):
        await serving

    assert (runner.finished, runner.cancelled) == (0, 1)
