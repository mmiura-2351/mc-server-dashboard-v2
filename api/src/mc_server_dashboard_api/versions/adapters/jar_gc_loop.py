"""Lifespan driver for the periodic JAR-pool GC (D4, issue #293).

Runs :class:`RunJarPoolGc` on a fixed cadence as an asyncio task on the FastAPI
event loop, mirroring the snapshot/backup loops. Kept out of the application layer
(pure orchestration / timing, not a use case) and free of any HTTP type.

The loop's resolution is ``tick_seconds``: it wakes that often and runs one full
sweep. The pool grows slowly (one entry per distinct resolved JAR), so a daily
cadence is the default. A failure inside one pass is logged and the loop
continues — one bad pass must not stop reclaiming for good. Cancelling the task
(on shutdown) ends the loop cleanly.
"""

from __future__ import annotations

import asyncio
import logging

from mc_server_dashboard_api.versions.application.jar_gc import RunJarPoolGc

_LOG = logging.getLogger(__name__)


async def run_jar_gc_loop(gc: RunJarPoolGc, *, tick_seconds: float) -> None:
    """Run one GC pass every ``tick_seconds`` until cancelled."""

    while True:
        # Sleep first so the initial tick is deferred by one full cadence.
        # A transient DB/worker outage at boot no longer causes a ~90-line
        # ERROR traceback on the very first tick (issue #1760).
        await asyncio.sleep(tick_seconds)
        try:
            await gc()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad pass must not kill the loop
            _LOG.exception("jar-pool GC pass failed; continuing")
