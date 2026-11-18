"""Shared execution semantics for periodic API lifespan tasks.

Callers keep ownership of their callback and error policy. This runner owns only
the startup delay, the recurring delay, exception survival, and cancellation
behavior that those tasks share.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Literal

PeriodicCallback = Callable[[], Awaitable[object]]
ErrorHandler = Callable[[], None]
FirstPass = Literal["soon", "after_interval", "immediately"]

# Cap on the random delay before a "soon" first pass (issue #3868).
_FIRST_PASS_MAX_DELAY_SECONDS = 600.0

# Draws the first-pass delay. Module-level so tests can pin the draw.
_RNG = random.Random()


async def run_periodic(
    callback: PeriodicCallback,
    *,
    tick_seconds: float,
    on_error: ErrorHandler,
    first_pass: FirstPass = "soon",
) -> None:
    """Run ``callback`` on a fixed cadence until cancelled.

    ``first_pass`` sets when the first pass runs; later passes follow
    ``tick_seconds``. Every delay runs inside the task, so none holds up startup
    or readiness.

    - ``"soon"`` (the default): after a delay drawn uniformly from
      ``[0, min(tick_seconds, 600))`` seconds. Waiting a full interval first
      meant an API restarted more often than the interval (on every deploy, say)
      never ran the pass at all (issue #3868). The random draw spreads API
      processes restarted together, so they do not all hit the store at once.
    - ``"after_interval"``: after one full ``tick_seconds``. For the storage
      sweep loop, whose pass the lifespan already runs on every start.
    - ``"immediately"``: at once. For the reconciler, whose startup reset must
      run before its first cadence wait.
    """

    if first_pass == "soon":
        window = min(tick_seconds, _FIRST_PASS_MAX_DELAY_SECONDS)
        await asyncio.sleep(_RNG.random() * window)
    elif first_pass == "after_interval":
        await asyncio.sleep(tick_seconds)

    while True:
        try:
            await callback()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad callback must not stop the task
            on_error()
        await asyncio.sleep(tick_seconds)
