"""Wiring test for the login-attempt prune lifespan task."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import pytest

from mc_server_dashboard_api.identity.adapters import prune_login_attempts_loop
from mc_server_dashboard_api.identity.application.prune_login_attempts import (
    PruneLoginAttempts,
)


class _SpyPruner:
    def __init__(self) -> None:
        self.ticks = 0

    async def tick(self) -> None:
        self.ticks += 1


async def test_wires_pruner_tick_and_interval_to_periodic_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pruner = _SpyPruner()
    runner = AsyncMock()
    monkeypatch.setattr(prune_login_attempts_loop, "run_periodic", runner)

    await prune_login_attempts_loop.run_prune_login_attempts_loop(
        cast(PruneLoginAttempts, pruner), tick_seconds=17
    )

    runner.assert_awaited_once()
    call = runner.await_args
    assert call is not None
    callback = call.args[0]
    assert call.kwargs["tick_seconds"] == 17
    assert callable(call.kwargs["on_error"])
    await callback()
    assert pruner.ticks == 1
