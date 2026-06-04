"""Authorization Ports for the community context (Section 6.4).

Two primitives the rest of the system depends on without knowing how the answer
is computed (NFR-PORT-1):

- :class:`MembershipVisibility` — the Layer-1 isolation check. A route asks
  :meth:`~MembershipVisibility.is_member` first and returns 404 (no existence
  signal) for non-members before any permission evaluation (FR-COMM-3).
- :class:`PermissionChecker` — the Layer-2 decision primitive
  ``can(user, operation, resource)`` (FR-AUTHZ-1). Business logic calls it and
  is unaware of role/grant set math behind it.

The intended two-layer usage is: non-member -> 404 (visibility); member without
the operation -> 403 (permission). Keeping these separate keeps the
no-existence-signal rule honest.
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    Permission,
    ResourceRef,
    UserId,
)


class MembershipVisibility(abc.ABC):
    """Port: the Layer-1 visibility/isolation check (FR-COMM-3, Section 6.4)."""

    @abc.abstractmethod
    async def is_member(self, *, user_id: UserId, community_id: CommunityId) -> bool:
        """Return whether ``user_id`` is a member of ``community_id``.

        Route handlers call this first: a ``False`` result must surface as 404
        (not 403), so non-members get no signal that the community exists.
        """


class PermissionChecker(abc.ABC):
    """Port: the ``can(user, operation, resource)`` decision (FR-AUTHZ-1)."""

    @abc.abstractmethod
    async def can(
        self, *, user: AuthUser, operation: Permission, resource: ResourceRef
    ) -> bool:
        """Return whether ``user`` may perform ``operation`` on ``resource``.

        The effective Layer-2 permission set is the union of the permissions of
        the member's roles in ``resource.community_id`` and the resource grants
        to that member matching the specific resource (FR-AUTHZ-2). Platform-admin
        operations are decided on ``user.is_platform_admin`` outside any Community
        context (FR-AUTHZ-5). ``operation`` is validated against the authoritative
        catalog (FR-AUTHZ-3); an uncatalogued code raises ``UnknownPermissionError``.
        """
