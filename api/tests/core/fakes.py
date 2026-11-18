"""Shared test doubles for the core context's tests."""

from __future__ import annotations

import asyncio


class RecordingSleep:
    """Fake ``asyncio.sleep``: records each delay, returns at once for the first
    ``returns`` calls, then parks until the task is cancelled."""

    def __init__(self, *, returns: int) -> None:
        self.delays: list[float] = []
        self.parked = asyncio.Event()
        self._returns = returns

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) > self._returns:
            self.parked.set()
            await asyncio.Event().wait()
