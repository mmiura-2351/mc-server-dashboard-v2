"""Pure brute-force / lockout math (SECURITY.md Section 2, FR-AUTH-4).

The sliding-window counting and lockout persistence are the
:class:`~.login_attempt_store.LoginAttemptStore` Port's job; this module holds
only the framework-free, deterministic decisions the use case makes from those
counts: whether an account is currently locked, and — when a failure crosses a
threshold — how long the next lockout lasts under exponential back-off.

The back-off doubles ``lockout_base_seconds`` on each repeat lockout of the same
account, capped at ``lockout_max_seconds`` (Section 2 step 4). ``lockout_count``
is the account's historic lockout count *before* this lockout, so the first
lockout uses the base duration.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class BruteForceConfig:
    """The brute-force knobs (CONFIGURATION.md Section 7.2), as a domain value."""

    enabled: bool
    username_threshold: int
    username_window: dt.timedelta
    ip_threshold: int
    ip_window: dt.timedelta
    lockout_base: dt.timedelta
    lockout_max: dt.timedelta
    delay: dt.timedelta


def is_locked(locked_until: dt.datetime | None, *, now: dt.datetime) -> bool:
    """Return whether an account lockout is still active at ``now``."""

    return locked_until is not None and locked_until > now


def backoff_duration(
    lockout_count: int, *, base: dt.timedelta, maximum: dt.timedelta
) -> dt.timedelta:
    """Lockout duration for the ``lockout_count``-th historic lockout.

    Doubles ``base`` once per prior lockout (``base * 2**lockout_count``), capped
    at ``maximum``. ``lockout_count`` is the count *before* this lockout, so the
    first lockout (count 0) is exactly ``base``.
    """

    duration = base * (2**lockout_count)
    return duration if duration < maximum else maximum
