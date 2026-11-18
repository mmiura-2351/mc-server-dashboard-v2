"""Adapter implementing the identity :class:`CommunityOwnership` Port over community.

The edge where an account deletion asks "does this user own any community?"
(FR-COMM-4). Only the *adapter* crosses contexts — the identity domain stays
unaware of how communities model ownership — and the question is answered by
community's own :func:`user_owns_any_community`, so what an Owner is and the
tables it is read from stay with the community context.

It runs on the identity unit of work's session, inside the delete transaction,
under the user's account write lock (``core/adapters/account_write_lock.py``,
#3217). A deletion reads this while it holds every active admin's ``user`` row,
so the answer is one statement, however many memberships the user has.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from mc_server_dashboard_api.community.adapters.repositories import (
    user_owns_any_community,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    UserId as CommunityUserId,
)
from mc_server_dashboard_api.core.adapters.account_write_lock import (
    take_account_write_lock,
)
from mc_server_dashboard_api.identity.domain.community_ownership import (
    CommunityOwnership,
)
from mc_server_dashboard_api.identity.domain.value_objects import UserId


class CommunityBackedOwnership(CommunityOwnership):
    """:class:`CommunityOwnership` backed by the community store on one session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def owns_any_community(self, user_id: UserId) -> bool:
        # Exclusive: a conferral of ownership on this user, a member addition
        # or a grant creation for them holds the lock shared until it commits,
        # so this waits for one in flight, and the read below -- a new
        # snapshot -- sees any ownership it committed.
        await take_account_write_lock(self._session, user_id.value, shared=False)
        return await user_owns_any_community(
            self._session, CommunityUserId(user_id.value)
        )
