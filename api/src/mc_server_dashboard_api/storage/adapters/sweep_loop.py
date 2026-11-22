"""Lifespan driver for the periodic crash-recovery storage sweep (issue #2252).

Runs the backend-agnostic ``storage.sweep`` on a fixed cadence as an asyncio
task on the FastAPI event loop, mirroring the JAR-pool / plugin-cache GC loops.
Until now the sweep ran only once, in the startup hook; on an interval it bounds
the accumulation of orphan staging/snapshot prefixes and orphan in-progress
multipart parts without a restart (STORAGE.md Section 9.5).

The sweep is dispatched exactly like the startup hook: awaited directly when it
is a coroutine function (the async object adapter) and run off the event loop
via a thread otherwise (the blocking fs adapter). Each pass then runs the
row-less backup archive reclaim (issue #4106), which reads the backup rows the
adapter's sweep cannot. A failure inside either is logged and the loop
continues — one bad pass must not stop reclaiming for good, and a sweep that
fails does not stop the reclaim. Cancelling the task (on shutdown) ends the loop
cleanly.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable

from mc_server_dashboard_api.core.adapters.periodic_runner import run_periodic

_LOG = logging.getLogger(__name__)

Sweep = Callable[[], Awaitable[None]] | Callable[[], None]


async def run_storage_sweep_loop(
    sweep: Sweep,
    *,
    reclaim_rowless_archives: Callable[[], Awaitable[None]],
    tick_seconds: float,
) -> None:
    """Run one sweep, then the reclaim, every ``tick_seconds`` until cancelled."""

    async def run_pass() -> None:
        try:
            if inspect.iscoroutinefunction(sweep):
                await sweep()
            else:
                await asyncio.to_thread(sweep)
        except Exception:  # noqa: BLE001 - the reclaim runs regardless
            _LOG.exception("storage sweep pass failed; continuing")
        await reclaim_rowless_archives()

    # One full interval first, unlike the GC loops (issue #3868): the lifespan
    # already sweeps and reclaims before serving on every start, so an early pass
    # would only repeat them.
    await run_periodic(
        run_pass,
        tick_seconds=tick_seconds,
        on_error=lambda: _LOG.exception(
            "row-less backup archive reclaim pass failed; continuing"
        ),
        first_pass="after_interval",
    )
