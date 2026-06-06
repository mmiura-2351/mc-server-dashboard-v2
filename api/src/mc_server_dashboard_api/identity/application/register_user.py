"""RegisterUser use case: create a new global user account (FR-AUTH-1).

Validates the password against the policy (SECURITY.md Section 1), pre-checks
username/email uniqueness for a friendly error, hashes the password, and
persists the user atomically through the :class:`UnitOfWork`. The pre-check is
not authoritative: the database's unique constraints are, and a concurrent
duplicate is translated to the same domain error by the unit of work on commit
(race-safe). The plaintext never leaves this call.

Open registration is unauthenticated by design, so it carries two operator abuse
controls (FR-AUTH-1, issue #362): a master switch (``RegistrationConfig.open``)
that turns self-registration off for an admin-provisioned deployment, and a
per-IP sliding-window cap that reuses FR-AUTH-4's ``login_attempt``-backed
counting (the :class:`LoginAttemptStore` Port) and the same trusted-proxy
client-IP resolution, rather than a parallel mechanism. Both are checked before a
user row is created; the per-IP path is skipped when no trustworthy client IP is
available, exactly as the login per-IP counter is (SECURITY.md Section 4).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import (
    EmailAlreadyExistsError,
    RegistrationDisabledError,
    RegistrationThrottledError,
    UsernameAlreadyExistsError,
)
from mc_server_dashboard_api.identity.domain.login_attempt_store import (
    LoginAttemptStore,
)
from mc_server_dashboard_api.identity.domain.password_hasher import PasswordHasher
from mc_server_dashboard_api.identity.domain.password_policy import PasswordPolicy
from mc_server_dashboard_api.identity.domain.registration import RegistrationConfig
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    UserId,
    Username,
)

# When the wiring does not configure registration controls, default to the
# historic behaviour: open registration, no per-IP cap.
_OPEN_UNLIMITED = RegistrationConfig(
    open=True,
    ip_limit_enabled=False,
    ip_threshold=0,
    ip_window=dt.timedelta(0),
)


@dataclass(frozen=True)
class RegisterUser:
    """Register a user, enforcing policy, uniqueness, and abuse controls."""

    uow: UnitOfWork
    hasher: PasswordHasher
    clock: Clock
    policy: PasswordPolicy
    # Optional so the historic 4-argument construction (and tests that do not
    # exercise the abuse controls) keep working; both default to the open,
    # unlimited posture.
    attempts: LoginAttemptStore | None = None
    registration: RegistrationConfig = field(default=_OPEN_UNLIMITED)

    async def __call__(
        self, *, username: str, email: str, password: str, ip: str | None = None
    ) -> User:
        if not self.registration.open:
            raise RegistrationDisabledError

        now = self.clock.now()
        await self._enforce_ip_limit(ip, now=now)

        name = Username(username)
        address = EmailAddress(email)
        self.policy.validate(password, username=name, email=address)

        user = User(
            id=UserId.new(),
            username=name,
            email=address,
            password_hash=self.hasher.hash(password),
            created_at=now,
            updated_at=now,
        )

        async with self.uow:
            if await self.uow.users.get_by_username(name) is not None:
                raise UsernameAlreadyExistsError(name.value)
            if await self.uow.users.get_by_email(address) is not None:
                raise EmailAlreadyExistsError(address.value)
            await self.uow.users.add(user)
            await self.uow.commit()
        return user

    async def _enforce_ip_limit(self, ip: str | None, *, now: dt.datetime) -> None:
        """Record the attempt and reject once the per-IP window cap is crossed.

        Skipped when the per-IP cap is disabled or no trustworthy client IP is
        available (the same posture as the login per-IP counter, SECURITY.md
        Section 4). The attempt is recorded *before* the count so a throttled
        flood keeps the window populated and the block persists.
        """

        if not self.registration.ip_limit_enabled or ip is None:
            return
        assert self.attempts is not None
        await self.attempts.record_registration(ip=ip, at=now)
        count = await self.attempts.count_ip_registrations(
            ip, since=now - self.registration.ip_window
        )
        if count > self.registration.ip_threshold:
            raise RegistrationThrottledError
