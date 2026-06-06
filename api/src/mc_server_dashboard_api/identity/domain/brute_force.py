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

from mc_server_dashboard_api.identity.domain.registration import RegistrationConfig


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


def prune_horizon(
    config: BruteForceConfig,
    registration: RegistrationConfig | None = None,
) -> dt.timedelta:
    """Oldest age a ``login_attempt`` row can still be relevant (Section 3).

    The sliding-window counts never look further back than the longest window, so
    a row older than that can never affect a decision. Both prune triggers — the
    on-success prune in the login use case and the periodic prune loop — delete
    rows older than ``now - prune_horizon(...)``; sharing this one computation
    keeps the bound identical between them.

    Registration per-IP rows (issue #362) share the ``login_attempt`` table but
    are counted over the *registration* per-IP window, which can outlast both
    login windows. When the registration cap is enabled its window is folded in
    so those rows survive their full window — otherwise a blanket prune at the
    login horizon would silently shrink the registration cap. A disabled cap (or
    no registration config) records no such rows, so it does not widen the bound.
    """

    horizon = max(config.username_window, config.ip_window)
    if registration is not None and registration.ip_limit_enabled:
        horizon = max(horizon, registration.ip_window)
    return horizon


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
