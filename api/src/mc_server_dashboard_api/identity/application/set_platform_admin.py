"""SetPlatformAdmin use case: a platform admin grants / revokes the admin flag.

This is what replaces the documented ``psql`` bootstrap step (DEPLOYMENT.md
Section 5) for everything past the very first admin (issue #278). Granting sets
``is_platform_admin`` true; revoking sets it false but refuses to revoke from the
last ACTIVE platform admin (:class:`LastPlatformAdminError`). Self-revoke is
allowed -- the last-active-admin guard is the only thing that stops an admin
demoting the final administrator, whether that is themselves or another.

No token rotation is needed: the access token's only identity claim is the user
id, not the admin flag, so a freshly revoked admin keeps a valid session but
simply fails the platform-admin gate on its next admin request.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.errors import (
    LastPlatformAdminError,
    UserNotFoundError,
)
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import UserId


@dataclass(frozen=True)
class SetPlatformAdmin:
    """Grant or revoke a target user's platform-admin flag (platform-admin only)."""

    uow: UnitOfWork
    clock: Clock

    async def __call__(self, *, target_id: UserId, grant: bool) -> None:
        async with self.uow:
            user = await self.uow.users.get_by_id(target_id)
            if user is None:
                raise UserNotFoundError(str(target_id.value))

            # Revoking an active admin reduces the set, so take a FOR UPDATE lock
            # so concurrent last-two-admin revokes serialize and exactly one wins
            # (#260). A grant never reduces the set, so it stays lock-free (the
            # short-circuit on ``not grant`` keeps the lock off the grant path).
            if (
                not grant
                and user.is_platform_admin
                and user.active
                and await self.uow.users.lock_active_platform_admins() <= 1
            ):
                raise LastPlatformAdminError(str(target_id.value))

            user.is_platform_admin = grant
            user.updated_at = self.clock.now()
            await self.uow.users.update(user)
            await self.uow.commit()
