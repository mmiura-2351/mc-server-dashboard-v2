"""Lifespan driver for the general-scheduler runner (epic #649, issue #1838).

Runs :meth:`RunScheduleTick.tick` on a fixed cadence as an asyncio task on the
FastAPI event loop, mirroring the backup loop. Kept out of the application layer
(pure orchestration / timing, not a use case) and free of any HTTP type.

The loop's resolution is ``tick_seconds``: it wakes that often and lets the
runner decide which schedules are due. A failure inside one tick is logged and
the loop continues — one bad tick must not stop the scheduler for the whole
fleet.

Shutdown drains the loop (issue #4109): :func:`drain_schedule_loop` sets
``stopping``, after which the loop takes no further tick, and waits up to
``drain_seconds`` for the tick in flight, so a scheduled backup that finishes in
time leaves its archive, its row and its run record. A tick still running after
the drain is cancelled, with one WARNING line. The backup it cancels deletes
the archive it wrote unless its row's COMMIT was sent (``CreateBackup``), and
its occurrence, unrecorded, runs again on the next boot.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from mc_server_dashboard_api.servers.application.schedule_runner import RunScheduleTick

_LOG = logging.getLogger(__name__)


async def run_schedule_loop(
    runner: RunScheduleTick, *, tick_seconds: float, stopping: asyncio.Event
) -> None:
    """Run ``runner.tick()`` every ``tick_seconds`` until ``stopping`` is set."""

    while True:
        # Sleep first so the initial tick is deferred by one full cadence, sparing
        # a boot-time DB/worker hiccup a first-tick traceback (the backup-loop
        # #1760 precedent). The sleep ends early once ``stopping`` is set.
        with suppress(TimeoutError):
            async with asyncio.timeout(tick_seconds):
                await stopping.wait()
        if stopping.is_set():
            return
        try:
            await runner.tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
            _LOG.exception("schedule runner tick failed; continuing")


async def drain_schedule_loop(
    loop: asyncio.Task[None], stopping: asyncio.Event, *, drain_seconds: float
) -> None:
    """Stop ``loop`` ticking, then give its tick in flight ``drain_seconds``.

    A tick still running after that is cancelled. Returns once the loop has
    ended.
    """

    stopping.set()
    _LOG.info("draining the schedule runner for up to %s s", drain_seconds)
    done, _ = await asyncio.wait({loop}, timeout=drain_seconds)
    if done:
        _LOG.info("schedule runner drained")
    else:
        _LOG.warning(
            "schedule runner drain timed out after %s s; cancelling its tick",
            drain_seconds,
        )
        loop.cancel()
    with suppress(asyncio.CancelledError):
        await loop
