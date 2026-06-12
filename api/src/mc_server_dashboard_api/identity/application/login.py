"""Login use case: verify credentials, enforce brute-force protection, issue a
token pair (FR-AUTH-2, FR-AUTH-4).

Looks up the user by username, verifies the password against the stored hash via
the :class:`PasswordHasher`, and on success issues an access + refresh pair,
persisting the refresh row atomically.

Around that it enforces the SECURITY.md Section 2 brute-force algorithm through
the :class:`LoginAttemptStore`: every attempt is recorded; an account already
locked, an IP over its sliding-window threshold, an unknown user, a wrong
password, and a deactivated account are *all* rejected as a single
:class:`InvalidCredentialsError` after
awaiting the artificial :class:`LoginFailureDelay`, so none can be told apart by
status or timing (enumeration defence). Crossing the per-username threshold locks
the account for an exponentially backed-off duration; a successful login clears
the lockout, resets the back-off, and prunes stale attempt rows.

Two enumeration-defence details: brute-force state keys on the case-folded
:attr:`Username.key`, so failures spread across spelling variants of one username
aggregate and lock together; and the unknown-user path still runs
:meth:`PasswordHasher.verify` against a static dummy hash so it costs the same as
the wrong-password path and cannot be told apart by timing.

The per-failure reason is recorded on the ``login_attempt`` row for forensics
only; it is never surfaced to the caller.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import NoReturn

from mc_server_dashboard_api.identity.application.issue_tokens import issue_token_pair
from mc_server_dashboard_api.identity.application.token_pair import TokenPair
from mc_server_dashboard_api.identity.domain.brute_force import (
    BruteForceConfig,
    backoff_duration,
    is_locked,
    prune_horizon,
)
from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.errors import InvalidCredentialsError
from mc_server_dashboard_api.identity.domain.login_attempt_store import (
    LoginAttemptStore,
)
from mc_server_dashboard_api.identity.domain.login_failure_delay import (
    LoginFailureDelay,
)
from mc_server_dashboard_api.identity.domain.password_hasher import PasswordHasher
from mc_server_dashboard_api.identity.domain.registration import RegistrationConfig
from mc_server_dashboard_api.identity.domain.token_service import TokenService
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import Username

# Failure reasons recorded on the ``login_attempt`` row (forensics only; never
# surfaced — the caller always sees one uniform error).
REASON_LOCKED = "locked"
REASON_IP_THROTTLED = "ip_throttled"
REASON_UNKNOWN_USER = "unknown_user"
REASON_WRONG_PASSWORD = "wrong_password"
REASON_DEACTIVATED = "deactivated"


@dataclass(frozen=True)
class LoginResult:
    """A successful login: the issued token pair plus the authenticated user id.

    The ``user_id`` lets the route attribute the ``auth:login`` SUCCESS audit row
    to the actor (FR-AUD-1) without re-querying. A login *failure* never surfaces
    a user id (enumeration defence, SECURITY.md Section 2), so it is carried only
    on the success result, not threaded through the error path.
    """

    pair: TokenPair
    user_id: uuid.UUID


@dataclass(frozen=True)
class Login:
    """Authenticate a username/password and mint a session token pair."""

    uow: UnitOfWork
    attempts: LoginAttemptStore
    brute_force: BruteForceConfig
    hasher: PasswordHasher
    dummy_password_hash: str
    tokens: TokenService
    clock: Clock
    failure_delay: LoginFailureDelay
    refresh_ttl: dt.timedelta
    # Registration per-IP rows (issue #362) share the ``login_attempt`` table; the
    # on-success prune folds the registration window into its horizon so it does
    # not evict still-counted registration rows. Optional/default-None so the
    # historic construction keeps working.
    registration: RegistrationConfig | None = None

    async def __call__(
        self, *, username: str, password: str, ip: str | None = None
    ) -> LoginResult:
        name = Username(username)
        key = name.key
        now = self.clock.now()

        if self.brute_force.enabled:
            blocked = await self._blocked_reason(key, ip, now=now)
            if blocked is not None:
                await self._fail(key, ip, blocked, now=now)

        async with self.uow:
            user = await self.uow.users.get_by_username(name)
            if user is None:
                # Verify against a static dummy hash so the unknown-user path
                # costs the same as a wrong password (timing-enumeration defence).
                await self.hasher.verify(password, self.dummy_password_hash)
                await self._fail(key, ip, REASON_UNKNOWN_USER, now=now)
            if not await self.hasher.verify(password, user.password_hash):
                await self._fail(key, ip, REASON_WRONG_PASSWORD, now=now)
            # A deactivated account fails *after* the password verify so it costs
            # the same as a wrong password and shares the uniform error: the
            # response and timing must not distinguish "deactivated" from "wrong
            # password" (enumeration posture, issue #278).
            if not user.active:
                await self._fail(key, ip, REASON_DEACTIVATED, now=now)
            pair = await issue_token_pair(
                uow=self.uow,
                tokens=self.tokens,
                user_id=user.id,
                now=now,
                refresh_ttl=self.refresh_ttl,
            )
            user_id = user.id.value
            await self.uow.commit()

        if self.brute_force.enabled:
            await self.attempts.record_attempt(
                username=key, ip=ip, success=True, failure_reason=None, at=now
            )
            await self.attempts.clear_lockout(key)
            await self._prune(now=now)
        return LoginResult(pair=pair, user_id=user_id)

    async def _blocked_reason(
        self, username: str, ip: str | None, *, now: dt.datetime
    ) -> str | None:
        """Reason to reject before verifying, or ``None``: lockout / IP threshold."""

        lockout = await self.attempts.get_lockout(username)
        if lockout is not None and is_locked(lockout.locked_until, now=now):
            return REASON_LOCKED
        if ip is not None:
            ip_failures = await self.attempts.count_ip_failures(
                ip, since=now - self.brute_force.ip_window
            )
            if ip_failures >= self.brute_force.ip_threshold:
                return REASON_IP_THROTTLED
        return None

    async def _fail(
        self, username: str, ip: str | None, reason: str, *, now: dt.datetime
    ) -> NoReturn:
        """Record the failure, lock the account if over threshold, delay, raise.

        The same exit for every failure path (locked, IP-throttled, unknown user,
        wrong password) so none is distinguishable by status or timing; the
        ``reason`` is persisted on the attempt row for forensics, never returned.
        """

        if self.brute_force.enabled:
            await self.attempts.record_attempt(
                username=username, ip=ip, success=False, failure_reason=reason, at=now
            )
            await self._maybe_lock(username, now=now)
        await self.failure_delay.apply()
        raise InvalidCredentialsError

    async def _maybe_lock(self, username: str, *, now: dt.datetime) -> None:
        """Lock the account once the per-username threshold is crossed.

        Skips re-locking an account whose lockout is already active so the
        back-off escalates once per lockout *event*, not on every attempt that
        arrives while the account is locked (SECURITY.md Section 2 step 4).
        """

        existing = await self.attempts.get_lockout(username)
        if existing is not None and is_locked(existing.locked_until, now=now):
            return
        failures = await self.attempts.count_username_failures(
            username, since=now - self.brute_force.username_window
        )
        if failures < self.brute_force.username_threshold:
            return
        prior = existing.lockout_count if existing is not None else 0
        duration = backoff_duration(
            prior,
            base=self.brute_force.lockout_base,
            maximum=self.brute_force.lockout_max,
        )
        await self.attempts.lock(
            username, locked_until=now + duration, lockout_count=prior + 1
        )

    async def _prune(self, *, now: dt.datetime) -> None:
        """Drop attempt rows older than the longest sliding window (Section 3)."""

        await self.attempts.prune_attempts(
            older_than=now - prune_horizon(self.brute_force, self.registration)
        )
