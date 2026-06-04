"""The ``LoginFailureDelay`` Port: the artificial-delay seam for failed logins.

SECURITY.md Section 2 step 5 requires every failed authentication to incur the
same delay so a caller cannot distinguish "no such user" from "wrong password"
by timing (username-enumeration defence). The :class:`~..application.login.Login`
use case calls it on every failure path; the M1 adapter applies the configured
``auth.brute_force.delay_ms`` via the :class:`~.sleeper.Sleeper` Port.
"""

from __future__ import annotations

import abc


class LoginFailureDelay(abc.ABC):
    """Port: applied on every failed login to flatten the failure-timing signal."""

    @abc.abstractmethod
    async def apply(self) -> None:
        """Incur the configured artificial failure delay (SECURITY.md 2 step 5)."""
