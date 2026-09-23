"""Wiring test for the snapshot-scheduler lifespan task."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import pytest

from mc_server_dashboard_api.servers.adapters import snapshot_loop
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    RunSnapshotCadenceTick,
)


class _SpyScheduler:
    def __init__(self) -> None:
        self.ticks = 0

    async def tick(self) -> None:
        self.ticks += 1


async def test_wires_scheduler_tick_and_interval_to_periodic_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _SpyScheduler()
    runner = AsyncMock()
    monkeypatch.setattr(snapshot_loop, "run_periodic", runner)

    await snapshot_loop.run_snapshot_loop(
        cast(RunSnapshotCadenceTick, scheduler), tick_seconds=17
    )

    runner.assert_awaited_once()
    call = runner.await_args
    assert call is not None
    callback = call.args[0]
    assert call.kwargs["tick_seconds"] == 17
    assert callable(call.kwargs["on_error"])
    await callback()
    assert scheduler.ticks == 1
