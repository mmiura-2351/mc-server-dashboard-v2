"""Real adapter for the :class:`LoginFailureDelay` Port (#57, FR-AUTH-4).

Every failed login incurs the same fixed artificial delay (``auth.brute_force.
delay_ms``, SECURITY.md Section 2 step 5) so a caller cannot distinguish "no such
user" from "wrong password" — or a locked account — by timing. The pause is an
awaitable through the :class:`Sleeper` Port (no event-loop blocking); the M1
sleeper is :func:`asyncio.sleep`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.login_failure_delay import (
    LoginFailureDelay,
)
from mc_server_dashboard_api.identity.domain.sleeper import Sleeper


@dataclass(frozen=True)
class FixedLoginFailureDelay(LoginFailureDelay):
    """:class:`LoginFailureDelay` adapter that sleeps a fixed ``delay`` per call."""

    delay: dt.timedelta
    sleeper: Sleeper

    async def apply(self) -> None:
        await self.sleeper.sleep(self.delay)
