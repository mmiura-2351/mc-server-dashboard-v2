"""Login use case: verify credentials, enforce brute-force protection, issue a
token pair (FR-AUTH-2, FR-AUTH-4).

Looks up the user by username, verifies the password against the stored hash via
the :class:`PasswordHasher`, and on success issues an access + refresh pair,
persisting the refresh row atomically.

Around that it enforces the SECURITY.md Section 2 brute-force algorithm through
the :class:`LoginAttemptStore`: every attempt is recorded; an account already
locked, an IP over its sliding-window threshold, an unknown user, and a wrong
password are *all* rejected as a single :class:`InvalidCredentialsError` after
awaiting the artificial :class:`LoginFailureDelay`, so none can be told apart by
status or timing (enumeration defence). Crossing the per-username threshold locks
the account for an exponentially backed-off duration; a successful login clears
the lockout, resets the back-off, and prunes stale attempt rows.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import NoReturn

from mc_server_dashboard_api.identity.application.issue_tokens import issue_token_pair
from mc_server_dashboard_api.identity.application.token_pair import TokenPair
from mc_server_dashboard_api.identity.domain.brute_force import (
    BruteForceConfig,
    backoff_duration,
    is_locked,
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
from mc_server_dashboard_api.identity.domain.token_service import TokenService
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import Username


@dataclass(frozen=True)
class Login:
    """Authenticate a username/password and mint a session token pair."""

    uow: UnitOfWork
    attempts: LoginAttemptStore
    brute_force: BruteForceConfig
    hasher: PasswordHasher
    tokens: TokenService
    clock: Clock
    failure_delay: LoginFailureDelay
    refresh_ttl: dt.timedelta

    async def __call__(
        self, *, username: str, password: str, ip: str | None = None
    ) -> TokenPair:
        name = Username(username)
        now = self.clock.now()

        if self.brute_force.enabled and await self._is_blocked(name.value, ip, now=now):
            await self._fail(name.value, ip, now=now)

        async with self.uow:
            user = await self.uow.users.get_by_username(name)
            if user is None or not self.hasher.verify(password, user.password_hash):
                await self._fail(name.value, ip, now=now)
            pair = await issue_token_pair(
                uow=self.uow,
                tokens=self.tokens,
                user_id=user.id,
                now=now,
                refresh_ttl=self.refresh_ttl,
            )
            await self.uow.commit()

        if self.brute_force.enabled:
            await self.attempts.record_attempt(
                username=name.value, ip=ip, success=True, at=now
            )
            await self.attempts.clear_lockout(name.value)
            await self._prune(now=now)
        return pair

    async def _is_blocked(
        self, username: str, ip: str | None, *, now: dt.datetime
    ) -> bool:
        """Whether to reject before verifying: active lockout or IP over threshold."""

        lockout = await self.attempts.get_lockout(username)
        if lockout is not None and is_locked(lockout.locked_until, now=now):
            return True
        if ip is not None:
            ip_failures = await self.attempts.count_ip_failures(
                ip, since=now - self.brute_force.ip_window
            )
            if ip_failures >= self.brute_force.ip_threshold:
                return True
        return False

    async def _fail(
        self, username: str, ip: str | None, *, now: dt.datetime
    ) -> NoReturn:
        """Record the failure, lock the account if over threshold, delay, raise.

        The same exit for every failure path (locked, IP-throttled, unknown user,
        wrong password) so none is distinguishable by status or timing.
        """

        if self.brute_force.enabled:
            await self.attempts.record_attempt(
                username=username, ip=ip, success=False, at=now
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

        longest = max(self.brute_force.username_window, self.brute_force.ip_window)
        await self.attempts.prune_attempts(older_than=now - longest)
