"""Registration abuse-control policy (FR-AUTH-1, issue #362).

Open registration (``POST /users``) is unauthenticated by design, so it needs
two operator controls that authentication's FR-AUTH-4 hardening does not cover:

- ``open`` — whether self-registration is accepted at all. A private deployment
  whose accounts are provisioned by the admin turns this off; the admin surface
  keeps creating accounts.
- a per-IP sliding-window cap so the endpoint cannot be scripted to flood the
  user table. It reuses the same trusted-proxy client-IP resolution and the same
  ``login_attempt``-backed sliding-window counting as FR-AUTH-4's per-IP login
  throttle, rather than a parallel mechanism.

This module holds only the framework-free configuration value; the counting and
the open/closed decision live in the :class:`RegisterUser` use case.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class RegistrationConfig:
    """The registration abuse-control knobs (CONFIGURATION.md Section 7.4)."""

    open: bool
    ip_limit_enabled: bool
    ip_threshold: int
    ip_window: dt.timedelta
