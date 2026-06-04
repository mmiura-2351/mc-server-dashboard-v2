"""Persistence Ports for the community context.

The ``<Entity>Repository`` interfaces (ARCHITECTURE.md Section 5.1) the domain
depends on; concrete async-SQLAlchemy adapters implement them. Lookups return
``None`` when absent rather than raising, so callers decide policy. The two
grant-sweep methods (:meth:`ResourceGrantRepository.delete_for_user_in_community`
and :meth:`ResourceGrantRepository.delete_for_resource`) are the use-case-driven
cleanup paths DATABASE.md Section 10 documents: ``resource_grant`` is keyed by
``user_id`` (not ``membership_id``) and ``resource_id`` carries no FK, so neither
member-removal nor single-server deletion sweeps grants by cascade.
"""

from __future__ import annotations

import abc
import uuid

from mc_server_dashboard_api.community.domain.entities import (
    Community,
    Membership,
    ResourceGrant,
    Role,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    CommunityName,
    MembershipId,
    ResourceGrantId,
    RoleId,
    UserId,
)


class CommunityRepository(abc.ABC):
    """Port: persistence for :class:`Community` aggregates."""

    @abc.abstractmethod
    async def add(self, community: Community) -> None:
        """Stage a new community for persistence within the current transaction."""

    @abc.abstractmethod
    async def get_by_id(self, community_id: CommunityId) -> Community | None:
        """Return the community with ``community_id``, or ``None`` if absent."""

    @abc.abstractmethod
    async def get_by_name(self, name: CommunityName) -> Community | None:
        """Return the community with ``name``, or ``None`` if absent."""

    @abc.abstractmethod
    async def update(self, community: Community) -> None:
        """Persist mutable fields of ``community`` (M1: its name, FR-COMM-1)."""

    @abc.abstractmethod
    async def delete(self, community_id: CommunityId) -> None:
        """Delete the community, cascading to its dependent rows (Section 10)."""


class MembershipRepository(abc.ABC):
    """Port: persistence for :class:`Membership` joins and their role assignments."""

    @abc.abstractmethod
    async def add(self, membership: Membership) -> None:
        """Stage a new membership for persistence within the current transaction."""

    @abc.abstractmethod
    async def get_by_id(self, membership_id: MembershipId) -> Membership | None:
        """Return the membership with ``membership_id``, or ``None`` if absent."""

    @abc.abstractmethod
    async def get_by_user_and_community(
        self, user_id: UserId, community_id: CommunityId
    ) -> Membership | None:
        """Return the membership for ``(user_id, community_id)``, or ``None``."""

    @abc.abstractmethod
    async def list_for_user(self, user_id: UserId) -> list[Membership]:
        """Return all of ``user_id``'s memberships (FR-MEM-4 view scoping)."""

    @abc.abstractmethod
    async def delete(self, membership_id: MembershipId) -> None:
        """Delete the membership, cascading its ``membership_role`` rows (§10)."""

    @abc.abstractmethod
    async def assign_role(self, membership_id: MembershipId, role_id: RoleId) -> None:
        """Stage a ``membership_role`` row assigning ``role_id`` to the membership."""

    @abc.abstractmethod
    async def list_role_ids(self, membership_id: MembershipId) -> list[RoleId]:
        """Return the ids of the roles assigned to ``membership_id``."""


class RoleRepository(abc.ABC):
    """Port: persistence for :class:`Role` aggregates."""

    @abc.abstractmethod
    async def add(self, role: Role) -> None:
        """Stage a new role for persistence within the current transaction."""

    @abc.abstractmethod
    async def get_by_id(self, role_id: RoleId) -> Role | None:
        """Return the role with ``role_id``, or ``None`` if absent."""

    @abc.abstractmethod
    async def list_for_community(self, community_id: CommunityId) -> list[Role]:
        """Return all roles defined in ``community_id``."""


class ResourceGrantRepository(abc.ABC):
    """Port: persistence for :class:`ResourceGrant` rows."""

    @abc.abstractmethod
    async def add(self, grant: ResourceGrant) -> None:
        """Stage a new resource grant for persistence within the transaction."""

    @abc.abstractmethod
    async def get_by_id(self, grant_id: ResourceGrantId) -> ResourceGrant | None:
        """Return the grant with ``grant_id``, or ``None`` if absent."""

    @abc.abstractmethod
    async def get_for_user_resource(
        self,
        user_id: UserId,
        community_id: CommunityId,
        resource_type: str,
        resource_id: uuid.UUID,
    ) -> ResourceGrant | None:
        """Return the grant for ``(user, community, resource_type, resource_id)``.

        ``community_id`` is part of the key so a grant can never satisfy a check
        scoped to a different community (FR-AUTHZ-4 defense-in-depth). Returns
        ``None`` when no matching grant exists.
        """

    @abc.abstractmethod
    async def delete_for_user_in_community(
        self, user_id: UserId, community_id: CommunityId
    ) -> None:
        """Delete all of ``user_id``'s grants in ``community_id`` (FR-MEM-3, §10).

        Called by the remove-member use case in the same transaction as the
        membership deletion, since grants FK ``user_id`` (not ``membership_id``).
        """

    @abc.abstractmethod
    async def delete_for_resource(
        self, resource_type: str, resource_id: uuid.UUID
    ) -> None:
        """Delete all grants on a specific resource (Section 10 server-delete sweep).

        ``resource_id`` carries no FK, so deleting the resource does not cascade;
        the resource-delete use case calls this in the same transaction.
        """
