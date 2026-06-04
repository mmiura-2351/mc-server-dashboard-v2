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

Before the first tick the loop runs :class:`ResetUnverifiableObservedStates`
once (issue #230): the reconciler is the sole consumer of the stale observed
cache and must not act before that reset has succeeded (#224's correctness
condition). Running it here — rather than inline in the lifespan body — restores
the pre-#227 boot posture: a momentarily unreachable DB at startup no longer
crashes the process, since a failed reset is logged and retried on the next
iteration (and the tick is skipped until it succeeds).
"""

from __future__ import annotations

import asyncio
import logging

from mc_server_dashboard_api.servers.application.reconciler import RunReconcilerTick
from mc_server_dashboard_api.servers.application.startup_reset import (
    ResetUnverifiableObservedStates,
)

_LOG = logging.getLogger(__name__)


async def run_reconciler_loop(
    reconciler: RunReconcilerTick,
    *,
    reset: ResetUnverifiableObservedStates,
    tick_seconds: float,
) -> None:
    """Run ``reconciler.tick()`` every ``tick_seconds`` until cancelled.

    The one-time ``reset`` runs as the first action and gates ticking: until it
    succeeds the loop retries it each iteration and skips the tick, so a DB that
    is briefly unreachable at startup never crashes the process and the
    reconciler never acts on the stale observed cache.
    """

    reset_done = False
    while True:
        try:
            if not reset_done:
                await reset()
                reset_done = True
            await reconciler.tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
            if reset_done:
                _LOG.exception("reconciler tick failed; continuing")
            else:
                _LOG.warning(
                    "startup observed-state reset failed; skipping tick, "
                    "will retry next iteration",
                    exc_info=True,
                )
        await asyncio.sleep(tick_seconds)
