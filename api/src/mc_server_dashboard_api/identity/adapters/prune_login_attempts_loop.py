"""Lifespan driver for the periodic ``login_attempt`` prune (SECURITY.md 3).

Runs :meth:`PruneLoginAttempts.tick` on a fixed cadence as an asyncio task on the
FastAPI event loop, mirroring the snapshot and backup loop drivers. Kept out of
the application layer (pure orchestration / timing, no use-case logic, no HTTP
type).

Unlike the snapshot and backup loops, this loop is **not** gated on the control
plane: it needs only the database (the one Port it drives), not a Worker channel,
so it must run on every API process to keep the table bounded even with the
control plane disabled. A failure inside one tick is logged and the loop
continues — a single failed delete must not stop pruning forever. Cancelling the
task (on shutdown) ends the loop cleanly.
"""

from __future__ import annotations

import asyncio
import logging

from mc_server_dashboard_api.identity.application.prune_login_attempts import (
    PruneLoginAttempts,
)

_LOG = logging.getLogger(__name__)


async def run_prune_login_attempts_loop(
    pruner: PruneLoginAttempts, *, tick_seconds: float
) -> None:
    """Run ``pruner.tick()`` every ``tick_seconds`` until cancelled."""

    while True:
        try:
            await pruner.tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
            _LOG.exception("login_attempt prune tick failed; continuing")
        await asyncio.sleep(tick_seconds)
