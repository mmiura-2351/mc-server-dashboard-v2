"""No-op adapter for the :class:`LoginFailureDelay` Port (M1 seam).

The brute-force / lockout feature (#57, FR-AUTH-4, SECURITY.md Section 2) owns
the real artificial delay. Until then this honest no-op fills the seam so the
:class:`~..application.login.Login` use case already calls the hook on every
failure; only this adapter changes when the delay is implemented.
"""

from __future__ import annotations

from mc_server_dashboard_api.identity.domain.login_failure_delay import (
    LoginFailureDelay,
)


class NoOpLoginFailureDelay(LoginFailureDelay):
    """:class:`LoginFailureDelay` adapter that does nothing (M1 placeholder)."""

    async def apply(self) -> None:
        return None
