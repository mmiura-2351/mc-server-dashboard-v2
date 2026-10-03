"""Persistence Ports for the community context.

The ``<Entity>Repository`` interfaces (ARCHITECTURE.md Section 5.1) the domain
depends on; concrete async-SQLAlchemy adapters implement them. Lookups return
``None`` when absent rather than raising, so callers decide policy. There is no
grant-sweep method: ``resource_grant`` cascades from both its membership and its
server (DATABASE.md Section 10), so member removal and server deletion remove the
grants in the database, whatever order a concurrent grant creation lands in.
"""

from __future__ import annotations

import abc
import datetime as dt
import uuid
from collections.abc import Sequence

from mc_server_dashboard_api.community.domain.entities import (
    Community,
    CommunitySummary,
    Membership,
    ResourceGrant,
    Role,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    CommunityName,
    MembershipId,
    Permission,
    ResourceGrantId,
    RoleId,
    RoleName,
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
    async def count_all(self) -> int:
        """Return the total number of communities (platform-admin listing, #489)."""

    @abc.abstractmethod
    async def list_summaries_page(
        self, *, limit: int, offset: int
    ) -> list[CommunitySummary]:
        """Return a page of communities with their member/server counts.

        Backs the platform-admin ``GET /admin/communities`` listing (#489):
        ordered by ``created_at``, each row enriched with its ``member_count`` and
        ``server_count`` computed in grouped queries (no N+1). The counts cross
        into the ``server`` table, which the community adapter already reaches.
        """

    @abc.abstractmethod
    async def update(self, community: Community) -> None:
        """Persist mutable fields of ``community`` (M1: its name, FR-COMM-1).

        Never an insert: a community a concurrent delete removed since the
        caller's pre-read raises :class:`CommunityNotFoundError` rather than
        writing nothing and reporting success (issue #2613).
        """

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
    async def list_for_community(self, community_id: CommunityId) -> list[Membership]:
        """Return all memberships in ``community_id`` (member listing, member:read)."""

    @abc.abstractmethod
    async def delete(self, membership_id: MembershipId) -> None:
        """Delete the membership, cascading its ``membership_role`` rows.

        See Section 10.
        """

    @abc.abstractmethod
    async def assign_role(self, membership_id: MembershipId, role_id: RoleId) -> None:
        """Stage a ``membership_role`` row assigning ``role_id`` to the membership."""

    @abc.abstractmethod
    async def unassign_role(self, membership_id: MembershipId, role_id: RoleId) -> None:
        """Delete the ``membership_role`` row assigning ``role_id`` to the member."""

    @abc.abstractmethod
    async def list_role_ids(self, membership_id: MembershipId) -> list[RoleId]:
        """Return the ids of the roles assigned to ``membership_id``."""

    @abc.abstractmethod
    async def lock_role_ids(self, membership_id: MembershipId) -> list[RoleId]:
        """Return :meth:`list_role_ids`, its assignments share-locked until commit.

        For a caller whose write depends on the member holding these roles
        (#3241): an unassignment or a cascade from a member removal or role
        deletion waits for this transaction, and an assignment such a deletion
        committed first is no longer returned.
        """

    @abc.abstractmethod
    async def lock_owner_role_holders(
        self, community_id: CommunityId, role_id: RoleId
    ) -> list[MembershipId]:
        """Lock and return the membership ids holding ``role_id`` in ``community_id``.

        Takes a ``SELECT ... FOR UPDATE`` lock on the ``membership_role`` rows
        that assign ``role_id`` to members of ``community_id``, serializing
        concurrent removals/unassignments so the second transaction blocks
        until the first commits and then re-evaluates the decremented set
        (#1959). Returns the membership ids of the current holders.
        """


class RoleRepository(abc.ABC):
    """Port: persistence for :class:`Role` aggregates."""

    @abc.abstractmethod
    async def add(self, role: Role) -> None:
        """Stage a new role for persistence within the current transaction."""

    @abc.abstractmethod
    async def get_by_id(self, role_id: RoleId) -> Role | None:
        """Return the role with ``role_id``, or ``None`` if absent."""

    @abc.abstractmethod
    async def get_by_ids(self, role_ids: Sequence[RoleId]) -> list[Role]:
        """Return the roles matching ``role_ids`` in one query (unknown ids skipped).

        Batches the authorization hot path's role lookups (issue #321). An empty
        ``role_ids`` returns ``[]`` without touching the store. Order is
        unspecified.
        """

    @abc.abstractmethod
    async def list_for_community(self, community_id: CommunityId) -> list[Role]:
        """Return all roles defined in ``community_id``."""

    @abc.abstractmethod
    async def lock_by_id(self, role_id: RoleId) -> Role | None:
        """Return the role with ``role_id`` locked until the transaction ends.

        For a caller whose write depends on the role's current state (#3215): a
        concurrent locker waits, then reads the row as this transaction left it.
        ``None`` if absent.
        """

    @abc.abstractmethod
    async def lock_by_ids(
        self, role_ids: Sequence[RoleId], *, for_update: RoleId | None = None
    ) -> list[Role]:
        """Lock the roles of ``role_ids`` and ``for_update``; return those that exist.

        ``for_update`` is locked as :meth:`lock_by_id` does, every other role
        share-locked: a concurrent edit or deletion of it waits for this
        transaction, and a locker that waited reads the role as committed
        (#3241). Rows are locked one at a time in ascending id order, each at its
        final strength, so transactions locking overlapping sets through this
        method cannot deadlock on them. Order of the result is unspecified.
        """

    @abc.abstractmethod
    async def update(
        self,
        role_id: RoleId,
        *,
        name: RoleName | None = None,
        permissions: set[Permission] | None = None,
        updated_at: dt.datetime,
    ) -> Role:
        """Write only the supplied columns (plus ``updated_at``); return the row.

        A column left ``None`` keeps its stored value, so an edit cannot restore
        what it read before a concurrent edit of another column committed
        (#3215). The returned role is the row as written, concurrent changes
        included.

        Never an insert: a role a concurrent delete removed since the caller's
        pre-read raises :class:`RoleNotFoundError` rather than writing nothing and
        reporting success (issue #2613).
        """

    @abc.abstractmethod
    async def delete(self, role_id: RoleId) -> None:
        """Delete the role, cascading its ``membership_role`` rows (Section 10)."""


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
    async def lock_for_user_resource(
        self,
        user_id: UserId,
        community_id: CommunityId,
        resource_type: str,
        resource_id: uuid.UUID,
    ) -> ResourceGrant | None:
        """Return :meth:`get_for_user_resource`, share-locked until commit.

        For a caller whose write depends on the grant (#3241): a revocation, or
        a cascade from a member removal or server deletion, waits for this
        transaction, and a grant one committed first is no longer returned.
        """

    @abc.abstractmethod
    async def list_for_community(
        self, community_id: CommunityId, user_id: UserId | None = None
    ) -> list[ResourceGrant]:
        """Return the community's grants, optionally filtered to ``user_id``.

        Backs the ``grant:read`` listing (community-wide, or per-member when
        ``user_id`` is given).
        """

    @abc.abstractmethod
    async def delete(self, grant_id: ResourceGrantId) -> None:
        """Delete the grant with ``grant_id`` (the ``grant:manage`` revoke)."""


class ResourceExistenceChecker(abc.ABC):
    """Port: does a grantable resource exist within a community? (issue #361).

    Grant creation validates that ``resource_id`` names a real resource in the
    community before persisting, so a fabricated id cannot become a ghost grant.
    The ``resource_grant`` FK to ``server`` (issue #3216) does not know the
    community, so this Port is what rejects a server of another community. The
    concrete adapter queries the owning context's table (M1: ``server``) and is
    bound on the unit of work's session, so the check runs inside the create
    transaction.
    """

    @abc.abstractmethod
    async def exists(
        self, community_id: CommunityId, resource_type: str, resource_id: uuid.UUID
    ) -> bool:
        """Return whether ``(resource_type, resource_id)`` is in ``community_id``.

        An existing resource is held against deletion until the transaction ends:
        the grant creation locks the actor's own grant on it afterwards (#3241),
        and a deletion, which removes the resource before cascading to its
        grants, would otherwise deadlock with it.
        """
