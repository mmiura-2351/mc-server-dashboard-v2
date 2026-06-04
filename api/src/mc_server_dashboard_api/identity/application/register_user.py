"""RegisterUser use case: create a new global user account (FR-AUTH-1).

Validates the password against the policy (SECURITY.md Section 1), pre-checks
username/email uniqueness for a friendly error, hashes the password, and
persists the user atomically through the :class:`UnitOfWork`. The pre-check is
not authoritative: the database's unique constraints are, and a concurrent
duplicate is translated to the same domain error by the unit of work on commit
(race-safe). The plaintext never leaves this call.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import (
    EmailAlreadyExistsError,
    UsernameAlreadyExistsError,
)
from mc_server_dashboard_api.identity.domain.password_hasher import PasswordHasher
from mc_server_dashboard_api.identity.domain.password_policy import PasswordPolicy
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    UserId,
    Username,
)


@dataclass(frozen=True)
class RegisterUser:
    """Register a user, enforcing policy and uniqueness."""

    uow: UnitOfWork
    hasher: PasswordHasher
    clock: Clock
    policy: PasswordPolicy

    async def __call__(self, *, username: str, email: str, password: str) -> User:
        name = Username(username)
        address = EmailAddress(email)
        self.policy.validate(password, username=name, email=address)

        now = self.clock.now()
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
