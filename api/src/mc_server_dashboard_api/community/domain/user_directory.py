"""The ``UserDirectory`` Port: the community context's narrow view of users.

Provisioning a community needs to confirm the initial owner is an existing user
(FR-COMM-2), but the community domain must never import the identity domain (the
user is referenced by id value only; DATABASE.md Section 5). This Port is that
seam: it answers "does this user id exist?" and resolves user ids to display
usernames for read models (member listings, issue #78), and is implemented at the
edge (adapters) against the identity user store. Keeping it this narrow means the
community context stays unaware of how users are stored or what else they carry.
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.community.domain.value_objects import UserId


class UserDirectory(abc.ABC):
    """Port: existence lookup for a user referenced by the community context."""

    @abc.abstractmethod
    async def exists(self, user_id: UserId) -> bool:
        """Return whether a user with ``user_id`` exists in the identity store."""

    @abc.abstractmethod
    async def usernames_for(self, user_ids: list[UserId]) -> dict[UserId, str]:
        """Resolve ``user_ids`` to their display usernames in one batch lookup.

        Returns a mapping for the ids that resolve; ids absent from the identity
        store are simply omitted from the result, so callers handle a missing
        username explicitly (issue #78). Implementations must answer in a single
        indexed query, never one lookup per id.
        """
