"""Lifespan driver for the periodic crash-recovery storage sweep (issue #2252).

Runs the backend-agnostic ``storage.sweep`` on a fixed cadence as an asyncio
task on the FastAPI event loop, mirroring the JAR-pool / plugin-cache GC loops.
Until now the sweep ran only once, in the startup hook; on an interval it bounds
the accumulation of orphan staging/snapshot prefixes and orphan in-progress
multipart parts without a restart (STORAGE.md Section 9.5).

The sweep is dispatched exactly like the startup hook: awaited directly when it
is a coroutine function (the async object adapter) and run off the event loop
via a thread otherwise (the blocking fs adapter). A failure inside one pass is
logged and the loop continues — one bad pass must not stop reclaiming for good.
Cancelling the task (on shutdown) ends the loop cleanly.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable

_LOG = logging.getLogger(__name__)

Sweep = Callable[[], Awaitable[None]] | Callable[[], None]


async def run_storage_sweep_loop(sweep: Sweep, *, tick_seconds: float) -> None:
    """Run one sweep every ``tick_seconds`` until cancelled."""

    while True:
        # Sleep first so the initial tick is deferred by one full cadence; the
        # startup hook already runs one sweep before this loop is created.
        await asyncio.sleep(tick_seconds)
        try:
            if inspect.iscoroutinefunction(sweep):
                await sweep()
            else:
                await asyncio.to_thread(sweep)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad pass must not kill the loop
            _LOG.exception("storage sweep pass failed; continuing")
