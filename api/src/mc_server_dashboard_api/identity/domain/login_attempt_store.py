"""The ``LoginAttemptStore`` Port: brute-force / lockout runtime state.

SECURITY.md Section 3 decides this auth-hardening state lives in the relational
database in two tables (``login_attempt`` append-only, ``account_lockout`` one
row per username) kept separate from the core entity model, reached only through
this Port (NFR-PORT-1). The brute-force use case depends on the Port; the M1
adapter is the DB-backed implementation, bound at the edge.

The Port exposes exactly what the Section 2 algorithm needs: append an attempt,
count failures over the per-username and per-IP sliding windows, read and write
the per-account lockout/back-off record, clear it on a successful login, and
prune attempts older than the longest window (the Section 3 cleanup story).
"""

from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class Lockout:
    """The ``account_lockout`` record: active lockout + historic count.

    ``locked_until`` is the instant the active lockout expires (``None`` if the
    account has a row only because it was locked before but is not locked now).
    ``lockout_count`` is how many times the account has ever been locked, driving
    the exponential back-off (SECURITY.md Section 2 step 4).
    """

    locked_until: dt.datetime | None
    lockout_count: int


class LoginAttemptStore(abc.ABC):
    """Port: persists and queries brute-force / lockout state (SECURITY.md 3)."""

    @abc.abstractmethod
    async def record_attempt(
        self,
        *,
        username: str,
        ip: str | None,
        success: bool,
        at: dt.datetime,
    ) -> None:
        """Append one authentication attempt to ``login_attempt``."""

    @abc.abstractmethod
    async def count_username_failures(
        self, username: str, *, since: dt.datetime
    ) -> int:
        """Count failed attempts for ``username`` at or after ``since``."""

    @abc.abstractmethod
    async def count_ip_failures(self, ip: str, *, since: dt.datetime) -> int:
        """Count failed attempts from source ``ip`` at or after ``since``."""

    @abc.abstractmethod
    async def get_lockout(self, username: str) -> Lockout | None:
        """Return the account's lockout record, or ``None`` if it has none."""

    @abc.abstractmethod
    async def lock(
        self, username: str, *, locked_until: dt.datetime, lockout_count: int
    ) -> None:
        """Upsert the account's lockout to ``locked_until`` with ``lockout_count``."""

    @abc.abstractmethod
    async def clear_lockout(self, username: str) -> None:
        """Clear the active lockout and reset the back-off count for the account.

        Called on a successful authentication (SECURITY.md Section 2 final note).
        """

    @abc.abstractmethod
    async def prune_attempts(self, *, older_than: dt.datetime) -> None:
        """Delete ``login_attempt`` rows older than ``older_than`` (Section 3)."""
