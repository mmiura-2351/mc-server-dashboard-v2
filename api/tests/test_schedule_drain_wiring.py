"""The lifespan shutdown drains the schedule loop (issue #4109).

These drive the real ``create_app`` lifespan with the control plane on, so the
schedule loop runs, over a runner whose tick the test holds open: the tick in
flight when the shutdown begins gets ``schedule.shutdown_drain_seconds`` to
finish, and is cancelled once that runs out. Each test sets the drain itself,
so neither depends on the default (0, which cancels at once). The loop's
cadence is cut to a hundredth of a second so the first tick starts at once.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
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
# A shutdown with a drain no longer than ``_DRAIN_SECONDS`` takes milliseconds
# when nothing is wrong.
_SHUTDOWN_BOUND = 10
# Ample for the held tick the first test releases after 0.2 s, and short enough
# that a loop which never sees its stop is cancelled well inside the bounds.
_DRAIN_SECONDS = "5"


class _HeldRunner:
    """A runner whose tick waits for ``release``.

    Each tick yields to the event loop once, as a real one does, so a loop that
    keeps ticking after its stop cannot starve the test's own timeouts.
    """

    def __init__(self) -> None:
        self.in_tick = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = 0
        self.cancelled = 0

    async def tick(self) -> None:
        self.in_tick.set()
        await asyncio.sleep(0)
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


class _App:
    """The real app's lifespan, run in a task so a test can begin its shutdown."""

    def __init__(self, runner: _HeldRunner) -> None:
        self._runner = runner
        self._leave = asyncio.Event()
        self.serving: asyncio.Task[None] | None = None

    async def _serve(self) -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            await self._leave.wait()

    async def start_mid_tick(self) -> asyncio.Task[None]:
        """Start the app and wait for its first tick to begin."""

        self.serving = asyncio.create_task(self._serve())
        await asyncio.wait_for(self._runner.in_tick.wait(), _STARTUP_BOUND)
        return self.serving

    def begin_shutdown(self) -> None:
        self._leave.set()

    async def finish(self) -> None:
        """Let the shutdown end, whatever the code under test does.

        It never cancels the lifespan. A lifespan cancelled mid-teardown leaves
        its gRPC server running, and at the session's end pytest then waits for
        a thread that server keeps alive, past every timeout. So once the bound
        passes, this releases the held tick instead: that ends a drain still
        waiting on it, and ``_DRAIN_SECONDS`` ends one whose loop never sees its
        stop.
        """

        if self.serving is None:
            return
        self._leave.set()
        _, pending = await asyncio.wait({self.serving}, timeout=_SHUTDOWN_BOUND)
        if pending:
            self._runner.release.set()
            await asyncio.wait({self.serving}, timeout=_SHUTDOWN_BOUND)


@pytest.fixture
async def app(runner: _HeldRunner) -> AsyncIterator[_App]:
    served = _App(runner)
    try:
        yield served
    finally:
        await served.finish()


async def test_a_shutdown_lets_the_scheduled_tick_in_flight_finish(
    runner: _HeldRunner, app: _App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_SCHEDULE__SHUTDOWN_DRAIN_SECONDS", _DRAIN_SECONDS)
    serving = await app.start_mid_tick()

    app.begin_shutdown()
    # The shutdown has begun, and the tick is still running.
    await asyncio.sleep(0.2)
    assert not serving.done()
    runner.release.set()
    await app.finish()

    assert serving.done()
    serving.result()
    assert (runner.finished, runner.cancelled) == (1, 0)


async def test_a_shutdown_cancels_the_scheduled_tick_once_its_drain_runs_out(
    runner: _HeldRunner, app: _App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCD_API_SCHEDULE__SHUTDOWN_DRAIN_SECONDS", "0")
    serving = await app.start_mid_tick()

    app.begin_shutdown()
    await app.finish()

    assert serving.done()
    serving.result()
    assert (runner.finished, runner.cancelled) == (0, 1)
