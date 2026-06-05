"""SetUserActive use case: a platform admin deactivates / reactivates an account.

Deactivation (``active=False``) keeps the user row -- so audit history and the
username/email uniqueness survive -- but makes the account unusable: every
outstanding refresh token is revoked here, and outstanding access tokens are
rejected by :class:`AuthenticateRequest` on their next request (it re-checks the
``active`` flag it already loads). The guards refuse:

- deactivating yourself -> :class:`SelfTargetError` (use the self-service routes);
- deactivating the last ACTIVE platform admin -> :class:`LastPlatformAdminError`.

Reactivation (``active=True``) simply clears the flag; it has no last-admin or
self guard (re-enabling an account never reduces the administrable set, and an
admin cannot reach this route while their own account is deactivated anyway).
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.errors import (
    LastPlatformAdminError,
    SelfTargetError,
    UserNotFoundError,
)
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import UserId


@dataclass(frozen=True)
class SetUserActive:
    """Deactivate or reactivate a target user's account (platform-admin only)."""

    uow: UnitOfWork
    clock: Clock

    async def __call__(
        self, *, actor_id: UserId, target_id: UserId, active: bool
    ) -> None:
        # NOTE: the last-active-admin guard below is check-then-act under READ
        # COMMITTED with no row lock, so concurrent deactivations could race past
        # it (last-admin TOCTOU). The non-transactional posture is kept on purpose
        # and tracked with the self-delete guard in #260.
        if not active and target_id == actor_id:
            raise SelfTargetError(str(target_id.value))

        async with self.uow:
            user = await self.uow.users.get_by_id(target_id)
            if user is None:
                raise UserNotFoundError(str(target_id.value))

            if not active:
                if (
                    user.is_platform_admin
                    and user.active
                    and await self.uow.users.count_active_platform_admins() <= 1
                ):
                    raise LastPlatformAdminError(str(target_id.value))

            user.active = active
            user.updated_at = self.clock.now()
            await self.uow.users.update(user)
            if not active:
                # Revoke every session so the deactivation takes effect on the
                # refresh path immediately, not only on access-token expiry.
                await self.uow.refresh_tokens.revoke_all_for_user(
                    user.id, revoked_at=self.clock.now()
                )
            await self.uow.commit()
