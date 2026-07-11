"""cronsim-backed ``NextRunCalculator`` adapter (epic #649, issue #1835).

Wraps ``cronsim`` (the healthchecks.io cron engine) behind the domain Port:
``CronSim`` iterates occurrences strictly after its start instant and handles
DST transitions Debian-cron style — a job in the spring-forward gap fires right
after the jump, a job in the repeated fall-back hour fires once. The cron
dependency stays confined here; the domain remains stdlib-only (the
import-linter contract pins that).
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from cronsim import CronSim, CronSimError

from mc_server_dashboard_api.servers.domain.errors import InvalidCronExpressionError
from mc_server_dashboard_api.servers.domain.next_run_calculator import (
    NextRunCalculator,
)

# Any fixed instant works for parse-only construction; CronSim validates the
# expression in its constructor before any iteration happens.
_PARSE_PROBE = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)


class CronsimNextRunCalculator(NextRunCalculator):
    """:class:`NextRunCalculator` adapter over ``cronsim``."""

    def validate(self, expr: str) -> None:
        try:
            CronSim(expr, _PARSE_PROBE)
        except CronSimError as exc:
            raise InvalidCronExpressionError(str(exc)) from exc

    def next_after(self, expr: str, tz: str, after: dt.datetime) -> dt.datetime:
        # Evaluate on local wall-clock time in the schedule's zone; CronSim
        # yields occurrences strictly after its start instant.
        local_after = after.astimezone(ZoneInfo(tz))
        try:
            occurrence = next(CronSim(expr, local_after))
        except CronSimError as exc:
            # Persisted expressions were validated at write time; surface a
            # typed error anyway rather than an opaque engine failure.
            raise InvalidCronExpressionError(str(exc)) from exc
        return occurrence.astimezone(dt.timezone.utc)
