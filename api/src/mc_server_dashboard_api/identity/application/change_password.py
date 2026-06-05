"""ChangePassword use case: the authenticated user changes their own password.

Verifies the supplied current password against the stored hash, enforces the
full registration password policy on the new password (SECURITY.md Section 1,
same reason codes), re-hashes, and revokes *all* of the user's refresh tokens so
every outstanding session is invalidated — the in-flight access token may ride
out its short TTL (documented at the route). All writes happen atomically through
the :class:`UnitOfWork`.

A wrong current password raises :class:`InvalidCredentialsError`, the same error
login raises, so the edge maps it to the same uniform 401 and a caller cannot use
this endpoint as a password-confirmation oracle. The current-password check is
*not* fed into the FR-AUTH-4 brute-force counters: those defend the unauthenticated
login surface against username enumeration, whereas this operation already sits
behind a valid access token, so it is not an enumeration oracle. The plaintext
never leaves this call.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.errors import (
    InvalidCredentialsError,
    UserNotFoundError,
)
from mc_server_dashboard_api.identity.domain.password_hasher import PasswordHasher
from mc_server_dashboard_api.identity.domain.password_policy import PasswordPolicy
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import UserId


@dataclass(frozen=True)
class ChangePassword:
    """Change the authenticated user's password, revoking their sessions."""

    uow: UnitOfWork
    hasher: PasswordHasher
    clock: Clock
    policy: PasswordPolicy

    async def __call__(
        self, *, user_id: UserId, current_password: str, new_password: str
    ) -> None:
        async with self.uow:
            user = await self.uow.users.get_by_id(user_id)
            if user is None:
                raise UserNotFoundError(str(user_id.value))
            if not self.hasher.verify(current_password, user.password_hash):
                raise InvalidCredentialsError
            self.policy.validate(new_password, username=user.username, email=user.email)

            now = self.clock.now()
            user.password_hash = self.hasher.hash(new_password)
            user.updated_at = now
            await self.uow.users.update(user)
            await self.uow.refresh_tokens.revoke_all_for_user(user.id, revoked_at=now)
            await self.uow.commit()
