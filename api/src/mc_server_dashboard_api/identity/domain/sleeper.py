"""The ``Sleeper`` Port: a non-blocking pause the artificial delay is built on.

A Port so the brute-force artificial delay (SECURITY.md Section 2 step 5) is an
*awaitable* pause — no event-loop blocking — and so tests can substitute a fake
that records the requested duration instead of really sleeping (TESTING.md
Section 4: never real-sleep in tests). The M1 adapter is :func:`asyncio.sleep`.
"""

from __future__ import annotations

import abc
import datetime as dt


class Sleeper(abc.ABC):
    """Port: asynchronously pause for ``duration`` without blocking the loop."""

    @abc.abstractmethod
    async def sleep(self, duration: dt.timedelta) -> None:
        """Await ``duration`` of wall-clock time."""
