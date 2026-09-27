"""Wiring test for the game-session prune lifespan task."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import pytest

from mc_server_dashboard_api.servers.adapters import game_session_prune_loop
from mc_server_dashboard_api.servers.application.game_sessions import PruneGameSessions


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
    monkeypatch.setattr(game_session_prune_loop, "run_periodic", runner)

    await game_session_prune_loop.run_game_session_prune_loop(
        cast(PruneGameSessions, pruner), tick_seconds=17
    )

    runner.assert_awaited_once()
    call = runner.await_args
    assert call is not None
    callback = call.args[0]
    assert call.kwargs["tick_seconds"] == 17
    assert callable(call.kwargs["on_error"])
    await callback()
    assert pruner.ticks == 1
