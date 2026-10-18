"""Unit tests for the abandoned reads of :func:`ws_database_read` (#3426).

A read abandoned at the bound, or when its caller is cancelled, is cancelled
but not awaited: against a frozen database its cleanup outlives the caller.
The event loop references tasks only weakly, so the module holds each one until
it finishes and its outcome has been consumed.
"""

from __future__ import annotations

import asyncio
import gc
from typing import Any

import pytest

from mc_server_dashboard_api import dependencies
from mc_server_dashboard_api.dependencies import (
    WsDatabaseUnavailableError,
    ws_database_read,
)


class _FrozenRead:
    """A read whose cancellation cleanup waits until the database ``thaw``s.

    ``late_error`` is what the cleanup raises on its way out, if anything.
    """

    def __init__(self, late_error: Exception | None = None) -> None:
        self.started = asyncio.Event()
        self.thawed = asyncio.Event()
        self._late_error = late_error

    async def __call__(self) -> object:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await self.thawed.wait()
            if self._late_error is not None:
                raise self._late_error from None
            raise
        raise AssertionError("unreachable")


async def _settle() -> None:
    """Let the thawed reads finish and their done callbacks run."""

    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _short_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies, "WS_DATABASE_TIMEOUT_SECONDS", 0.05)


async def test_an_abandoned_read_is_held_until_it_finishes() -> None:
    read = _FrozenRead()

    with pytest.raises(WsDatabaseUnavailableError):
        await ws_database_read(read, outcome="test")
    gc.collect()
    assert len(dependencies._abandoned_reads) == 1
    (task,) = dependencies._abandoned_reads
    assert not task.done()

    read.thawed.set()
    await _settle()
    assert dependencies._abandoned_reads == set()
    assert task.cancelled()


async def test_a_late_error_of_an_abandoned_read_is_consumed() -> None:
    loop = asyncio.get_running_loop()
    reported: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        read = _FrozenRead(late_error=ConnectionResetError("transport lost"))
        with pytest.raises(WsDatabaseUnavailableError):
            await ws_database_read(read, outcome="test")

        read.thawed.set()
        await _settle()
        assert dependencies._abandoned_reads == set()
        # The task is now unreferenced: collecting it would report an
        # exception nobody retrieved.
        gc.collect()
        await _settle()
    finally:
        loop.set_exception_handler(previous)
    assert reported == []


async def test_caller_cancellation_abandons_the_read_without_waiting_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dependencies, "WS_DATABASE_TIMEOUT_SECONDS", 60.0)
    read = _FrozenRead()
    caller = asyncio.create_task(ws_database_read(read, outcome="test"))
    await read.started.wait()

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, timeout=1)
    assert len(dependencies._abandoned_reads) == 1
    (task,) = dependencies._abandoned_reads
    assert not task.done()

    read.thawed.set()
    await _settle()
    assert dependencies._abandoned_reads == set()
