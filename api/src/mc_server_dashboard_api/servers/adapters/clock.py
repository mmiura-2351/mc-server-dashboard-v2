"""System-clock adapter for the servers :class:`Clock` Port (ARCHITECTURE.md 5.1)."""

from __future__ import annotations

import datetime as dt
import time

from mc_server_dashboard_api.servers.domain.clock import Clock


class SystemClock(Clock):
    """:class:`Clock` adapter backed by the wall clock, in UTC."""

    def now(self) -> dt.datetime:
        return dt.datetime.now(tz=dt.timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()
