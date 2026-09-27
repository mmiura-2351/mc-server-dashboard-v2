"""Shared execution semantics for periodic API lifespan tasks.

Callers keep ownership of their callback and error policy. This runner owns only
the recurring delay, exception survival, and cancellation behavior that those
tasks share.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

PeriodicCallback = Callable[[], Awaitable[object]]
ErrorHandler = Callable[[], None]


async def run_periodic(
    callback: PeriodicCallback,
    *,
    tick_seconds: float,
    on_error: ErrorHandler,
    run_immediately: bool = False,
) -> None:
    """Run ``callback`` on a fixed cadence until cancelled.

    The default first delay keeps ordinary lifespan tasks quiet during startup.
    ``run_immediately`` exists for the reconciler, whose startup reset must run
    before its first cadence wait.
    """

    if not run_immediately:
        await asyncio.sleep(tick_seconds)

    while True:
        try:
            await callback()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad callback must not stop the task
            on_error()
        await asyncio.sleep(tick_seconds)
