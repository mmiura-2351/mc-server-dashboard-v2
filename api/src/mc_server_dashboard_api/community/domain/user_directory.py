"""The ``UserDirectory`` Port: the community context's narrow view of users.

Provisioning a community needs to confirm the initial owner is an existing user
(FR-COMM-2), but the community domain must never import the identity domain (the
user is referenced by id value only; DATABASE.md Section 5). This Port is that
seam: it answers only "does this user id exist?", and is implemented at the edge
(adapters) against the identity user store. Keeping it this narrow means the
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
