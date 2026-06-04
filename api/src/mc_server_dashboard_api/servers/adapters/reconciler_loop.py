"""Lifespan driver for the periodic divergence reconciler (issue #101).

Runs :class:`RunReconcilerTick` on a fixed cadence as an asyncio task on the
FastAPI event loop, mirroring the snapshot and backup loops. Kept out of the
application layer (pure orchestration / timing, not a use case) and free of any
HTTP type.

The loop's resolution is ``tick_seconds``: it wakes that often and lets the
reconciler decide which servers are diverged and due to act on. A failure inside
one tick is logged and the loop continues — one bad tick must not stop the
reconciler for the whole fleet. Cancelling the task (on shutdown) ends the loop
cleanly.
"""

from __future__ import annotations

import asyncio
import logging

from mc_server_dashboard_api.servers.application.reconciler import RunReconcilerTick

_LOG = logging.getLogger(__name__)


async def run_reconciler_loop(
    reconciler: RunReconcilerTick, *, tick_seconds: float
) -> None:
    """Run ``reconciler.tick()`` every ``tick_seconds`` until cancelled."""

    while True:
        try:
            await reconciler.tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
            _LOG.exception("reconciler tick failed; continuing")
        await asyncio.sleep(tick_seconds)
