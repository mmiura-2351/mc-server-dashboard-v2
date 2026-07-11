"""Lifespan driver for the general-scheduler runner (epic #649, issue #1838).

Runs :meth:`RunScheduleTick.tick` on a fixed cadence as an asyncio task on the
FastAPI event loop, mirroring the backup loop. Kept out of the application layer
(pure orchestration / timing, not a use case) and free of any HTTP type.

The loop's resolution is ``tick_seconds``: it wakes that often and lets the
runner decide which schedules are due. A failure inside one tick is logged and
the loop continues — one bad tick must not stop the scheduler for the whole
fleet. Cancelling the task (on shutdown) ends the loop cleanly.
"""

from __future__ import annotations

import asyncio
import logging

from mc_server_dashboard_api.servers.application.schedule_runner import RunScheduleTick

_LOG = logging.getLogger(__name__)


async def run_schedule_loop(runner: RunScheduleTick, *, tick_seconds: float) -> None:
    """Run ``runner.tick()`` every ``tick_seconds`` until cancelled."""

    while True:
        # Sleep first so the initial tick is deferred by one full cadence, sparing
        # a boot-time DB/worker hiccup a first-tick traceback (the backup-loop
        # #1760 precedent).
        await asyncio.sleep(tick_seconds)
        try:
            await runner.tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
            _LOG.exception("schedule runner tick failed; continuing")
