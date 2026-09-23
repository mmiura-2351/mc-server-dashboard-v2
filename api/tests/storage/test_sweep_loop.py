"""Wiring tests for the storage crash-recovery sweep lifespan task."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from mc_server_dashboard_api.storage.adapters import sweep_loop


async def test_wires_an_async_sweep_to_periodic_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = 0

    async def sweep() -> None:
        nonlocal runs
        runs += 1

    runner = AsyncMock()
    to_thread = AsyncMock()
    monkeypatch.setattr(sweep_loop, "run_periodic", runner)
    monkeypatch.setattr(asyncio, "to_thread", to_thread)

    await sweep_loop.run_storage_sweep_loop(sweep, tick_seconds=17)

    runner.assert_awaited_once()
    call = runner.await_args
    assert call is not None
    callback = call.args[0]
    assert call.kwargs["tick_seconds"] == 17
    assert callable(call.kwargs["on_error"])
    await callback()
    assert runs == 1
    to_thread.assert_not_awaited()


async def test_wires_a_sync_sweep_through_a_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = 0

    def sweep() -> None:
        nonlocal runs
        runs += 1

    async def to_thread(callback: object) -> None:
        assert callable(callback)
        callback()

    runner = AsyncMock()
    to_thread_spy = AsyncMock(side_effect=to_thread)
    monkeypatch.setattr(sweep_loop, "run_periodic", runner)
    monkeypatch.setattr(asyncio, "to_thread", to_thread_spy)

    await sweep_loop.run_storage_sweep_loop(sweep, tick_seconds=17)

    runner.assert_awaited_once()
    call = runner.await_args
    assert call is not None
    callback = call.args[0]
    assert call.kwargs["tick_seconds"] == 17
    assert callable(call.kwargs["on_error"])
    await callback()
    assert runs == 1
    to_thread_spy.assert_awaited_once_with(sweep)
