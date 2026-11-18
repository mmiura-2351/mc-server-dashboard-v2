"""DeleteAccount use case: the authenticated user deletes their own account.

Re-authenticates the caller before this destructive self-service action: the
supplied current password is verified against the stored hash, mirroring
:class:`ChangePassword` (same hasher Port, same uniform failure). A mismatch
raises :class:`InvalidCredentialsError` — the same error login raises — so the
edge maps it to the same uniform 401 and the endpoint is not a password-
confirmation oracle (SECURITY.md Section 2). The verify runs outside any
transaction (#3861), so the delete transaction checks that the account, as it
locks it, still has the hash that was verified; a password changed in between
is refused the same way.

Refuses (FR-COMM-4 / FR-AUTH-6) when the user still owns a community — deleting
would orphan it — or is the last platform administrator; both are domain errors
the edge maps to 409. Otherwise it deletes the user row and revokes their refresh
tokens atomically. The user's memberships, role assignments, and resource grants
are removed by the ``ON DELETE CASCADE`` foreign keys on ``user.id`` (DATABASE.md
Sections 4-6), and the refresh tokens would cascade too; they are revoked
explicitly first so the row is consistent even before the cascade and the intent
is auditable.

The ownership check goes through the :class:`CommunityOwnership` seam so the
identity context never imports the community context. The seam is bound on the
unit of work's session, so the check runs inside the delete transaction, under
the account write lock, which a conferral of community ownership on the account,
a member addition and a grant creation for it wait for (#3217, #3856).
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.clock import Clock
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
    hasher: PasswordHasher
    clock: Clock

    async def __call__(self, *, user_id: UserId, password: str) -> None:
        # Re-authenticate before this destructive action: a wrong password is the
        # same InvalidCredentialsError login raises, mapped to the same uniform
        # 401 at the edge, so the check gates before any refusal reason leaks. The
        # read is a short transaction of its own, and the verify runs after it
        # ends: the KDF is slow and memory-hard, and no pooled connection or lock
        # may wait on it (#3861). The delete reopens the uow.
        async with self.uow:
            actor = await self.uow.users.get_by_id(user_id)
        if actor is None:
            raise UserNotFoundError(str(user_id.value))
        if not await self.hasher.verify(password, actor.password_hash):
            raise InvalidCredentialsError

        async with self.uow:
            # The invariant counts ACTIVE admins only (issue #278): a deactivated
            # admin cannot act, so it does not keep the platform administrable.
            # Decide from the account as locked with the active admins, so a
            # concurrent grant, deactivation or removal either commits first and
            # is seen, or waits for this delete (#260, #3239); an account
            # deactivated since it authenticated no longer counts.
            user, active_admins = await self.uow.users.lock_with_active_admins(user_id)
            if user is None:
                raise UserNotFoundError(str(user_id.value))
            # The password was verified against the hash read before this lock: a
            # change committed since makes that verify stale, so it is refused
            # like a wrong password, before any refusal reason (#3861).
            if user.password_hash != actor.password_hash:
                raise InvalidCredentialsError
            # Under the account write lock, held until this delete commits: a
            # conferral of ownership on the account, a member addition or a
            # grant creation either committed first and is seen here, or waits
            # and then finds no such user (#3217, #3856).
            if await self.uow.ownership.owns_any_community(user.id):
                raise CommunityOwnedError(str(user_id.value))
            if user.is_platform_admin and user.active and active_admins <= 1:
                raise LastPlatformAdminError(str(user_id.value))

            await self.uow.refresh_tokens.revoke_all_for_user(
                user.id, revoked_at=self.clock.now()
            )
            await self.uow.users.delete(user.id)
            await self.uow.commit()
