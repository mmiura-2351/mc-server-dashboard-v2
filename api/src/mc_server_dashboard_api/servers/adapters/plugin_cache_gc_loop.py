"""Lifespan driver for the periodic plugin-cache GC (issue #1332).

Runs :class:`RunPluginCacheGc` on a fixed cadence as an asyncio task on the
FastAPI event loop, mirroring the JAR-pool GC loop. A failure inside one pass
is logged and the loop continues. Cancelling the task (on shutdown) ends the
loop cleanly.
"""

from __future__ import annotations

import logging

from mc_server_dashboard_api.core.adapters.periodic_runner import run_periodic
from mc_server_dashboard_api.servers.application.plugin_cache_gc import (
    RunPluginCacheGc,
)

_LOG = logging.getLogger(__name__)


async def run_plugin_cache_gc_loop(
    gc: RunPluginCacheGc, *, tick_seconds: float
) -> None:
    """Run one GC pass every ``tick_seconds`` until cancelled."""

    await run_periodic(
        gc,
        tick_seconds=tick_seconds,
        on_error=lambda: _LOG.exception("plugin-cache GC pass failed; continuing"),
    )
