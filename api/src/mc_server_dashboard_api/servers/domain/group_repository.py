"""Persistence Port for player groups + their server attachments (issue #276).

The ``GroupRepository`` interface (ARCHITECTURE.md Section 5.1) the group use
cases depend on; a concrete async-SQLAlchemy adapter implements it. Groups are
community-scoped; players are stored as rows under a group (DATABASE.md), and
attachments are the many-to-many join between groups and servers. Lookups return
``None``/empty when absent rather than raising, so callers decide policy.
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.servers.domain.groups import (
    GroupId,
    GroupKind,
    GroupName,
    PlayerGroup,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId


class GroupRepository(abc.ABC):
    """Port: persistence for :class:`PlayerGroup` aggregates + attachments."""

    @abc.abstractmethod
    async def add(self, group: PlayerGroup) -> None:
        """Stage a new group (with its players) within the current transaction."""

    @abc.abstractmethod
    async def get_by_id(self, group_id: GroupId) -> PlayerGroup | None:
        """Return the group with ``group_id`` (players included), or ``None``."""

    @abc.abstractmethod
    async def get_by_community_kind_name(
        self, community_id: CommunityId, kind: GroupKind, name: GroupName
    ) -> PlayerGroup | None:
        """Return the group named ``name`` of ``kind`` in the community, or ``None``."""

    @abc.abstractmethod
    async def list_for_community(self, community_id: CommunityId) -> list[PlayerGroup]:
        """Return every group in ``community_id`` (the ``group:read`` listing)."""

    @abc.abstractmethod
    async def save(self, group: PlayerGroup) -> None:
        """Persist a group's mutable state: its name and its full player set.

        The player set is replaced wholesale (delete-then-insert) so an upsert /
        remove on the in-memory aggregate is mirrored to the rows in one call.
        """

    @abc.abstractmethod
    async def delete(self, group_id: GroupId) -> None:
        """Delete the group, its players, and its attachments (DATABASE.md cascade)."""

    @abc.abstractmethod
    async def attach(self, group_id: GroupId, server_id: ServerId) -> None:
        """Attach ``group_id`` to ``server_id`` (idempotent: a re-attach is a no-op)."""

    @abc.abstractmethod
    async def detach(self, group_id: GroupId, server_id: ServerId) -> bool:
        """Detach ``group_id`` from ``server_id``; return whether a row was removed."""

    @abc.abstractmethod
    async def is_attached(self, group_id: GroupId, server_id: ServerId) -> bool:
        """Return whether ``group_id`` is currently attached to ``server_id``."""

    @abc.abstractmethod
    async def list_server_ids_for_group(self, group_id: GroupId) -> list[ServerId]:
        """Return the ids of every server ``group_id`` is attached to."""

    @abc.abstractmethod
    async def list_groups_for_server(self, server_id: ServerId) -> list[PlayerGroup]:
        """Return every group attached to ``server_id`` (players included)."""

    @abc.abstractmethod
    async def list_groups_for_server_kind(
        self, server_id: ServerId, kind: GroupKind
    ) -> list[PlayerGroup]:
        """Return the groups of ``kind`` attached to ``server_id``, ordered by id.

        The sync step merges these into the regenerated ops.json / whitelist.json;
        a stable order keeps :func:`merge_players`' first-wins tie-break
        deterministic (issue #276).
        """
