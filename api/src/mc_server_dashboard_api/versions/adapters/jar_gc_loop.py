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

import logging

from mc_server_dashboard_api.core.adapters.periodic_runner import run_periodic
from mc_server_dashboard_api.versions.application.jar_gc import RunJarPoolGc

_LOG = logging.getLogger(__name__)


async def run_jar_gc_loop(gc: RunJarPoolGc, *, tick_seconds: float) -> None:
    """Run one GC pass every ``tick_seconds`` until cancelled."""

    await run_periodic(
        gc,
        tick_seconds=tick_seconds,
        on_error=lambda: _LOG.exception("jar-pool GC pass failed; continuing"),
    )
