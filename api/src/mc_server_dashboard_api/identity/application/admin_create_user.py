"""AdminCreateUser use case: a platform admin provisions an account (issue #368).

Closing self-registration (``auth.registration.open=false``, issue #362) must not
lock a deployment out of account provisioning. This is the admin-gated creation
path: it shares the validation, hashing, uniqueness pre-check, and atomic persist
of open registration through :func:`persist_new_user`, but carries none of the
open endpoint's abuse controls -- it is not gated by the open flag and never
touches the per-IP registration cap. The route layer (platform-admin only)
attributes the creation to the admin in the audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.application.register_user import persist_new_user
from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.password_hasher import PasswordHasher
from mc_server_dashboard_api.identity.domain.password_policy import PasswordPolicy
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class AdminCreateUser:
    """Create a user account on behalf of a platform admin (no abuse controls)."""

    uow: UnitOfWork
    hasher: PasswordHasher
    clock: Clock
    policy: PasswordPolicy

    async def __call__(self, *, username: str, email: str, password: str) -> User:
        return await persist_new_user(
            uow=self.uow,
            hasher=self.hasher,
            policy=self.policy,
            username=username,
            email=email,
            password=password,
            now=self.clock.now(),
        )
