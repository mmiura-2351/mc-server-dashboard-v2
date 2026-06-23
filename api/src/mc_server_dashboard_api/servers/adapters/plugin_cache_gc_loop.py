"""Lifespan driver for the periodic plugin-cache GC (issue #1332).

Runs :class:`RunPluginCacheGc` on a fixed cadence as an asyncio task on the
FastAPI event loop, mirroring the JAR-pool GC loop. A failure inside one pass
is logged and the loop continues. Cancelling the task (on shutdown) ends the
loop cleanly.
"""

from __future__ import annotations

import asyncio
import logging

from mc_server_dashboard_api.servers.application.plugin_cache_gc import (
    RunPluginCacheGc,
)

_LOG = logging.getLogger(__name__)


async def run_plugin_cache_gc_loop(
    gc: RunPluginCacheGc, *, tick_seconds: float
) -> None:
    """Run one GC pass every ``tick_seconds`` until cancelled."""

    while True:
        try:
            await gc()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad pass must not kill the loop
            _LOG.exception("plugin-cache GC pass failed; continuing")
        await asyncio.sleep(tick_seconds)
