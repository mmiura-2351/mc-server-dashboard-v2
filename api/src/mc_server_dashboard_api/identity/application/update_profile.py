"""UpdateProfile use case: the authenticated user edits their username / email.

Either field is optional; an omitted field is left unchanged. Each supplied value
is validated by its value object (blank/structural rules) and pre-checked for
case-insensitive uniqueness against *other* users, mirroring registration and
raising the same :class:`UsernameAlreadyExistsError` / :class:`EmailAlreadyExistsError`
the edge maps to 409. The pre-check is not authoritative — the database's unique
constraints are — and resubmitting one's own current value is not a conflict.

No token rotation is needed: the JWT access token's only identity claim is the
user id (``sub``), which never changes here, so outstanding tokens stay valid.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import (
    EmailAlreadyExistsError,
    UsernameAlreadyExistsError,
    UserNotFoundError,
)
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    UserId,
    Username,
)


@dataclass(frozen=True)
class UpdateProfile:
    """Update the authenticated user's username and/or email."""

    uow: UnitOfWork
    clock: Clock

    async def __call__(
        self, *, user_id: UserId, username: str | None, email: str | None
    ) -> User:
        new_name = Username(username) if username is not None else None
        new_email = EmailAddress(email) if email is not None else None

        async with self.uow:
            user = await self.uow.users.get_by_id(user_id)
            if user is None:
                raise UserNotFoundError(str(user_id.value))

            # An empty PATCH (no fields supplied) is a no-op: return the current
            # profile without committing, so updated_at is not bumped on no change.
            if new_name is None and new_email is None:
                return user

            if new_name is not None and new_name != user.username:
                if await self.uow.users.get_by_username(new_name) is not None:
                    raise UsernameAlreadyExistsError(new_name.value)
            if new_email is not None and new_email != user.email:
                if await self.uow.users.get_by_email(new_email) is not None:
                    raise EmailAlreadyExistsError(new_email.value)

            # Write only the profile columns (#3214): the read above holds no
            # lock, so persisting the whole entity would restore any security
            # state (active, admin flag, password hash) changed since.
            updated = await self.uow.users.update_profile(
                user_id,
                username=new_name,
                email=new_email,
                updated_at=self.clock.now(),
            )
            if updated is None:
                raise UserNotFoundError(str(user_id.value))
            await self.uow.commit()
        return updated
