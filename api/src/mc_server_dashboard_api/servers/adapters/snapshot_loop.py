"""Lifespan driver for the periodic snapshot scheduler (FR-DATA-7).

Runs :class:`RunSnapshotCadenceTick` on a fixed cadence as an asyncio task on the
FastAPI event loop, mirroring how the gRPC server and the Storage orphan sweep
are started in the app factory's lifespan. Kept out of the application layer (it
is pure orchestration / timing, not a use case) and free of any HTTP type.

The loop's resolution is ``tick_seconds``: it wakes that often and lets the
scheduler decide which servers are due. A tick proportionate to the floor keeps
the effective RPO close to each server's configured interval without busy-waking
(CONFIGURATION.md Section 5.4). A failure inside one tick is logged and the loop
continues — one bad tick must not stop snapshots for the whole fleet. Cancelling
the task (on shutdown) ends the loop cleanly.
"""

from __future__ import annotations

import logging

from mc_server_dashboard_api.core.adapters.periodic_runner import run_periodic
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    RunSnapshotCadenceTick,
)

_LOG = logging.getLogger(__name__)


async def run_snapshot_loop(
    scheduler: RunSnapshotCadenceTick, *, tick_seconds: float
) -> None:
    """Run ``scheduler.tick()`` every ``tick_seconds`` until cancelled."""

    await run_periodic(
        scheduler.tick,
        tick_seconds=tick_seconds,
        on_error=lambda: _LOG.exception("snapshot scheduler tick failed; continuing"),
    )
