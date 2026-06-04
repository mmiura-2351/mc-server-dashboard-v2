"""``asyncio.sleep`` adapter for the :class:`Sleeper` Port (ARCHITECTURE.md 5.1)."""

from __future__ import annotations

import asyncio
import datetime as dt

from mc_server_dashboard_api.identity.domain.sleeper import Sleeper


class AsyncioSleeper(Sleeper):
    """:class:`Sleeper` adapter backed by :func:`asyncio.sleep` (non-blocking)."""

    async def sleep(self, duration: dt.timedelta) -> None:
        await asyncio.sleep(duration.total_seconds())
