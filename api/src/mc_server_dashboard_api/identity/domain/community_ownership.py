"""The ``CommunityOwnership`` Port: identity's narrow view of community ownership.

Account deletion, self-service and administrator alike (FR-COMM-4), must refuse
a user who still owns a community, but the identity domain must never import the
community context (it references a user by id value only; the dependency points
the other way). This Port is that seam: it answers the single question "does
this user own any community?" and is implemented at the edge against the
community store. Keeping it this narrow means identity stays unaware of how
communities, roles, or memberships are modelled.

The answer must hold until the delete commits (#3217). The delete cascades the
user's memberships and role assignments, so ownership conferred between the
check and the delete would be deleted with it, leaving its community without an
Owner. The Port is therefore bound on the unit of work's session, so it is
answered inside the delete transaction, and the answer is taken under the
user's *community-ownership lock*, which every conferral of ownership takes too.
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.identity.domain.value_objects import UserId


class CommunityOwnership(abc.ABC):
    """Port: ownership lookup for a user the identity context wants to delete."""

    @abc.abstractmethod
    async def owns_any_community(self, user_id: UserId) -> bool:
        """Return whether ``user_id`` owns (holds the Owner role in) any community.

        First takes ``user_id``'s community-ownership lock, exclusively, held
        until the transaction ends. Every conferral of ownership on the user --
        a community provisioned with them as its Owner, the Owner role assigned
        to them -- takes the same lock, shared, as the first statement of its own
        transaction and only then checks that the user exists. So a conferral
        either committed before this call, and the answer counts the ownership it
        conferred, or it waits until this transaction has ended and then finds
        the user gone if it deleted them.

        A deletion calls this after step 1 of the identity lock order (the
        ``user`` rows, :class:`~...repositories.RefreshTokenRepository`) and
        before step 2.
        """
