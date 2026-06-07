"""DeleteAccount use case: the authenticated user deletes their own account.

Re-authenticates the caller before this destructive self-service action: the
supplied current password is verified against the stored hash, mirroring
:class:`ChangePassword` (same hasher Port, same uniform failure). A mismatch
raises :class:`InvalidCredentialsError` — the same error login raises — so the
edge maps it to the same uniform 401 and the endpoint is not a password-
confirmation oracle (SECURITY.md Section 2).

Refuses (FR-COMM-4 / FR-AUTH-6) when the user still owns a community — deleting
would orphan it — or is the last platform administrator; both are domain errors
the edge maps to 409. Otherwise it deletes the user row and revokes their refresh
tokens atomically. The user's memberships, role assignments, and resource grants
are removed by the ``ON DELETE CASCADE`` foreign keys on ``user.id`` (DATABASE.md
Sections 4-6), and the refresh tokens would cascade too; they are revoked
explicitly first so the row is consistent even before the cascade and the intent
is auditable.

The ownership check goes through the :class:`CommunityOwnership` seam so the
identity context never imports the community context.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.community_ownership import (
    CommunityOwnership,
)
from mc_server_dashboard_api.identity.domain.errors import (
    CommunityOwnedError,
    InvalidCredentialsError,
    LastPlatformAdminError,
    UserNotFoundError,
)
from mc_server_dashboard_api.identity.domain.password_hasher import PasswordHasher
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import UserId


@dataclass(frozen=True)
class DeleteAccount:
    """Delete the authenticated user's account, guarding orphaned resources."""

    uow: UnitOfWork
    ownership: CommunityOwnership
    hasher: PasswordHasher
    clock: Clock

    async def __call__(self, *, user_id: UserId, password: str) -> None:
        # Re-authenticate before this destructive action: a wrong password is the
        # same InvalidCredentialsError login raises, mapped to the same uniform
        # 401 at the edge, so the check gates before any refusal reason leaks. The
        # read is a short transaction of its own; the delete reopens the uow.
        async with self.uow:
            actor = await self.uow.users.get_by_id(user_id)
            if actor is None:
                raise UserNotFoundError(str(user_id.value))
            if not self.hasher.verify(password, actor.password_hash):
                raise InvalidCredentialsError

        # The community-owner check still runs in its own committed transaction
        # (a separate context's seam), so its TOCTOU window is unchanged; #260
        # closes only the last-active-admin race, which lives in this context.
        if await self.ownership.owns_any_community(user_id):
            raise CommunityOwnedError(str(user_id.value))

        async with self.uow:
            user = await self.uow.users.get_by_id(user_id)
            if user is None:
                raise UserNotFoundError(str(user_id.value))
            # The invariant counts ACTIVE admins only (issue #278): a deactivated
            # admin cannot act, so it does not keep the platform administrable.
            # The self-deleting user is itself active (it passed get_current_user),
            # so a count of 1 means it is the last active admin. lock_active_*
            # takes a FOR UPDATE lock so concurrent last-two-admin self-deletes
            # serialize and exactly one wins (#260); only this admin path locks.
            if (
                user.is_platform_admin
                and await self.uow.users.lock_active_platform_admins() <= 1
            ):
                raise LastPlatformAdminError(str(user_id.value))

            await self.uow.refresh_tokens.revoke_all_for_user(
                user.id, revoked_at=self.clock.now()
            )
            await self.uow.users.delete(user.id)
            await self.uow.commit()
