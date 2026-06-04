"""Lifespan driver for the periodic scheduled-backup scheduler (FR-BAK-3).

Runs :class:`RunBackupScheduleTick` on a fixed cadence as an asyncio task on the
FastAPI event loop, mirroring the snapshot loop. Kept out of the application layer
(pure orchestration / timing, not a use case) and free of any HTTP type.

The loop's resolution is ``tick_seconds``: it wakes that often and lets the
scheduler decide which servers are due. Backup cadence is measured in hours, so a
coarse tick suffices. A failure inside one tick is logged and the loop continues —
one bad tick must not stop backups for the whole fleet. Cancelling the task (on
shutdown) ends the loop cleanly.
"""

from __future__ import annotations

import asyncio
import logging

from mc_server_dashboard_api.servers.application.backup_scheduler import (
    RunBackupScheduleTick,
)

_LOG = logging.getLogger(__name__)


async def run_backup_loop(
    scheduler: RunBackupScheduleTick, *, tick_seconds: float
) -> None:
    """Run ``scheduler.tick()`` every ``tick_seconds`` until cancelled."""

    while True:
        try:
            await scheduler.tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
            _LOG.exception("backup scheduler tick failed; continuing")
        await asyncio.sleep(tick_seconds)
