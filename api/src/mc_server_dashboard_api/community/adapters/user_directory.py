"""Adapter implementing the community :class:`UserDirectory` Port over identity.

This is the edge where the community context's narrow "does this user exist?"
question (FR-COMM-2) is answered against the identity user store. Only the
*adapter* crosses contexts — the community domain stays unaware of identity
(DATABASE.md Section 5) — and it asks only for existence, nothing more.
"""

from __future__ import annotations

from mc_server_dashboard_api.community.domain.user_directory import UserDirectory
from mc_server_dashboard_api.community.domain.value_objects import UserId
from mc_server_dashboard_api.identity.domain.unit_of_work import (
    UnitOfWork as IdentityUnitOfWork,
)
from mc_server_dashboard_api.identity.domain.value_objects import (
    UserId as IdentityUserId,
)


class IdentityUserDirectory(UserDirectory):
    """:class:`UserDirectory` backed by the identity user repository."""

    def __init__(self, uow: IdentityUnitOfWork) -> None:
        self._uow = uow

    async def exists(self, user_id: UserId) -> bool:
        async with self._uow as uow:
            user = await uow.users.get_by_id(IdentityUserId(user_id.value))
        return user is not None
