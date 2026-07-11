"""Lifespan driver for the game-session retention prune (RELAY.md Section 8).

Runs :meth:`PruneGameSessions.tick` on a fixed cadence as an asyncio task on the
FastAPI event loop, mirroring the ``login_attempt`` prune loop. Kept out of the
application layer (pure orchestration / timing, no use-case logic, no HTTP type).

Started only when ``relay.enabled`` (the relay is what populates the table); a
failure inside one tick is logged and the loop continues — a single failed delete
must not stop pruning forever. Cancelling the task (on shutdown) ends the loop
cleanly.
"""

from __future__ import annotations

import asyncio
import logging

from mc_server_dashboard_api.servers.application.game_sessions import PruneGameSessions

_LOG = logging.getLogger(__name__)


async def run_game_session_prune_loop(
    pruner: PruneGameSessions, *, tick_seconds: float
) -> None:
    """Run ``pruner.tick()`` every ``tick_seconds`` until cancelled."""

    while True:
        # Sleep first so the initial tick is deferred by one full cadence.
        # A transient DB/worker outage at boot no longer causes a ~90-line
        # ERROR traceback on the very first tick (issue #1760).
        await asyncio.sleep(tick_seconds)
        try:
            await pruner.tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
            _LOG.exception("game_session prune tick failed; continuing")
