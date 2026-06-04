"""The ``LoginFailureDelay`` Port: the artificial-delay seam for failed logins.

SECURITY.md Section 2 step 5 requires every failed authentication to incur the
same delay so a caller cannot distinguish "no such user" from "wrong password"
by timing (username-enumeration defence). The brute-force / lockout feature
(#57, FR-AUTH-4) owns the real delay; this milestone only carves the seam so the
:class:`~..application.login.Login` use case calls it on every failure. The M1
adapter is an honest no-op.
"""

from __future__ import annotations

import abc


class LoginFailureDelay(abc.ABC):
    """Port: applied on every failed login to flatten the failure-timing signal."""

    @abc.abstractmethod
    async def apply(self) -> None:
        """Incur the configured artificial delay (no-op until #57 fills it in)."""
