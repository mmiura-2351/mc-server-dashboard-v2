"""System-clock adapter for the versions :class:`Clock` Port (ARCHITECTURE.md 5.1)."""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.versions.domain.clock import Clock


class SystemClock(Clock):
    """:class:`Clock` adapter backed by the wall clock, in UTC."""

    def now(self) -> dt.datetime:
        return dt.datetime.now(tz=dt.timezone.utc)
