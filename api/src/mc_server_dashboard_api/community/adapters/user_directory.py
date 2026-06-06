"""Adapter implementing the community :class:`UserDirectory` Port over identity.

This is the edge where the community context's user-directory questions are
answered against the identity user store: "does this user exist?" (FR-COMM-2),
"which user has this exact username?" so a non-admin owner can add a member
(issue #355), and "what are these users' display names?" for member listings
(issue #78). Only the *adapter* crosses contexts — the community domain stays
unaware of identity (DATABASE.md Section 5) — and it asks only for existence and
usernames.
"""

from __future__ import annotations

from mc_server_dashboard_api.community.domain.user_directory import UserDirectory
from mc_server_dashboard_api.community.domain.value_objects import UserId
from mc_server_dashboard_api.identity.domain.errors import InvalidUsernameError
from mc_server_dashboard_api.identity.domain.unit_of_work import (
    UnitOfWork as IdentityUnitOfWork,
)
from mc_server_dashboard_api.identity.domain.value_objects import (
    UserId as IdentityUserId,
)
from mc_server_dashboard_api.identity.domain.value_objects import Username


class IdentityUserDirectory(UserDirectory):
    """:class:`UserDirectory` backed by the identity user repository."""

    def __init__(self, uow: IdentityUnitOfWork) -> None:
        self._uow = uow

    async def exists(self, user_id: UserId) -> bool:
        async with self._uow as uow:
            user = await uow.users.get_by_id(IdentityUserId(user_id.value))
        return user is not None

    async def resolve_username(self, username: str) -> UserId | None:
        try:
            name = Username(username)
        except InvalidUsernameError:
            return None  # a blank name matches no account; treat as no match
        async with self._uow as uow:
            user = await uow.users.get_by_username(name)
        return UserId(user.id.value) if user is not None else None

    async def usernames_for(self, user_ids: list[UserId]) -> dict[UserId, str]:
        async with self._uow as uow:
            resolved = await uow.users.usernames_by_id(
                [IdentityUserId(uid.value) for uid in user_ids]
            )
        return {
            UserId(identity_id.value): username.value
            for identity_id, username in resolved.items()
        }
