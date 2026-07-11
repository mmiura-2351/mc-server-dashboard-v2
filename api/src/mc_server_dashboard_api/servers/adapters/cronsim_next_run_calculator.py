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
        # The Port contract is 5-field expressions only. cronsim would also
        # accept a 6-field (seconds-granularity) form, but applies its
        # Debian-cron DST fixup only to 5-field expressions, so a persisted
        # 6-field expression would silently get different DST semantics.
        if len(expr.split()) != 5:
            raise InvalidCronExpressionError("expected a 5-field cron expression")
        try:
            CronSim(expr, _PARSE_PROBE)
        except CronSimError as exc:
            raise InvalidCronExpressionError(str(exc)) from exc

    def next_after(self, expr: str, tz: str, after: dt.datetime) -> dt.datetime:
        if after.tzinfo is None:
            # Fail loudly like the interval path (aware-minus-naive TypeError)
            # rather than silently reinterpreting as process-local time.
            raise TypeError("after must be timezone-aware")
        # Evaluate on local wall-clock time in the schedule's zone; CronSim
        # yields occurrences strictly after its start *local wall-clock* time.
        local_after = after.astimezone(ZoneInfo(tz))
        try:
            iterator = CronSim(expr, local_after)
            while True:
                occurrence = next(iterator).astimezone(dt.timezone.utc)
                # cronsim's Debian DST fixup strips tzinfo (losing the fold)
                # and re-attaches fold=0, so in the fall-back repeated hour an
                # occurrence can map to a UTC instant at or before ``after``
                # (``after`` on the fold=1 pass, the occurrence on the fold=0
                # pass half an hour earlier). Skip until the Port's
                # strictly-after contract holds in UTC.
                if occurrence > after:
                    return occurrence
        except CronSimError as exc:
            # Persisted expressions were validated at write time; surface a
            # typed error anyway rather than an opaque engine failure.
            raise InvalidCronExpressionError(str(exc)) from exc
