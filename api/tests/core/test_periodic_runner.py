"""Tests for the shared periodic background-task runner."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from mc_server_dashboard_api.core.adapters.periodic_runner import run_periodic


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_waits_before_the_first_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_calls = 0
    sleep_started = asyncio.Event()
    keep_sleeping = asyncio.Event()

    async def callback() -> None:
        nonlocal callback_calls
        callback_calls += 1

    async def sleep(_: float) -> None:
        sleep_started.set()
        await keep_sleeping.wait()

    monkeypatch.setattr(asyncio, "sleep", sleep)
    task = asyncio.create_task(run_periodic(callback, tick_seconds=1, on_error=Mock()))

    await sleep_started.wait()

    assert callback_calls == 0
    await _cancel(task)


async def test_repeats_the_callback_after_each_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_calls = 0
    first_sleep_started = asyncio.Event()
    allow_first_sleep = asyncio.Event()
    second_sleep_started = asyncio.Event()
    allow_second_sleep = asyncio.Event()
    second_callback_done = asyncio.Event()
    keep_later_sleeps_waiting = asyncio.Event()
    sleep_calls = 0

    async def callback() -> None:
        nonlocal callback_calls
        callback_calls += 1
        if callback_calls == 2:
            second_callback_done.set()

    async def sleep(_: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            first_sleep_started.set()
            await allow_first_sleep.wait()
        elif sleep_calls == 2:
            second_sleep_started.set()
            await allow_second_sleep.wait()
        else:
            await keep_later_sleeps_waiting.wait()

    monkeypatch.setattr(asyncio, "sleep", sleep)
    task = asyncio.create_task(run_periodic(callback, tick_seconds=1, on_error=Mock()))

    await first_sleep_started.wait()
    allow_first_sleep.set()
    await second_sleep_started.wait()
    allow_second_sleep.set()
    await second_callback_done.wait()

    assert callback_calls == 2
    await _cancel(task)


async def test_continues_after_a_callback_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_calls = 0
    first_sleep_started = asyncio.Event()
    allow_first_sleep = asyncio.Event()
    second_sleep_started = asyncio.Event()
    allow_second_sleep = asyncio.Event()
    second_callback_done = asyncio.Event()
    keep_later_sleeps_waiting = asyncio.Event()
    sleep_calls = 0
    on_error = Mock()

    async def callback() -> None:
        nonlocal callback_calls
        callback_calls += 1
        if callback_calls == 1:
            raise RuntimeError("boom")
        second_callback_done.set()

    async def sleep(_: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            first_sleep_started.set()
            await allow_first_sleep.wait()
        elif sleep_calls == 2:
            second_sleep_started.set()
            await allow_second_sleep.wait()
        else:
            await keep_later_sleeps_waiting.wait()

    monkeypatch.setattr(asyncio, "sleep", sleep)
    task = asyncio.create_task(
        run_periodic(callback, tick_seconds=1, on_error=on_error)
    )

    await first_sleep_started.wait()
    allow_first_sleep.set()
    await second_sleep_started.wait()
    allow_second_sleep.set()
    await second_callback_done.wait()

    assert callback_calls == 2
    on_error.assert_called_once_with()
    await _cancel(task)


async def test_cancellation_interrupts_a_pending_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep_started = asyncio.Event()
    keep_sleeping = asyncio.Event()

    async def callback() -> None:
        raise AssertionError("callback must not run")

    async def sleep(_: float) -> None:
        sleep_started.set()
        await keep_sleeping.wait()

    monkeypatch.setattr(asyncio, "sleep", sleep)
    task = asyncio.create_task(run_periodic(callback, tick_seconds=1, on_error=Mock()))

    await sleep_started.wait()

    await _cancel(task)
    assert task.cancelled()


async def test_can_run_the_initial_callback_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_started = asyncio.Event()
    keep_callback_running = asyncio.Event()
    sleep_started = asyncio.Event()

    async def callback() -> None:
        callback_started.set()
        await keep_callback_running.wait()

    async def sleep(_: float) -> None:
        sleep_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "sleep", sleep)
    task = asyncio.create_task(
        run_periodic(
            callback,
            tick_seconds=1,
            on_error=Mock(),
            run_immediately=True,
        )
    )

    await callback_started.wait()

    assert not sleep_started.is_set()
    await _cancel(task)
