"""The ``CommunityOwnership`` Port: identity's narrow view of community ownership.

Self-deletion (FR-COMM-4) must refuse a user who still owns a community, but the
identity domain must never import the community context (it references a user by
id value only; the dependency points the other way). This Port is that seam: it
answers the single question "does this user own any community?" and is
implemented at the edge against the community store. Keeping it this narrow means
identity stays unaware of how communities, roles, or memberships are modelled.
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.identity.domain.value_objects import UserId


class CommunityOwnership(abc.ABC):
    """Port: ownership lookup for a user the identity context wants to delete."""

    @abc.abstractmethod
    async def owns_any_community(self, user_id: UserId) -> bool:
        """Return whether ``user_id`` owns (holds the Owner role in) any community."""
