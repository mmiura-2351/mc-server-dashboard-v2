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
        """Add the group and its players; the group row's INSERT runs in this call.

        The player rows are staged: the adapter flushes only the ``player_group``
        row here, so that row's constraints are enforced inside this call rather
        than at the unit of work's commit. A concurrent create of the same
        ``(community_id, kind, name)`` raises :class:`GroupNameAlreadyExistsError`
        (``uq_player_group_community_kind_name``, issue #2000), while
        ``fk_player_group_community_id_community`` is enforced at the same flush
        but untranslated, so a community deleted mid-create surfaces as a raw
        ``IntegrityError`` (issue #2924).
        """

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

        Never an insert: a group a concurrent delete removed since the caller's
        pre-read raises :class:`GroupNotFoundError` rather than writing nothing
        and reporting success (issue #2613).

        Nothing is left for the unit of work's commit: the adapter executes the
        player-row DELETE and flushes the replacement rows itself, so the
        violations surface inside this call. The DELETE autoflushes the pending
        name UPDATE, so a racer that took the target
        ``(community_id, kind, name)`` first raises
        :class:`GroupNameAlreadyExistsError`
        (``uq_player_group_community_kind_name``, issue #2000).

        The flush of the replacement ``group_player`` rows carries two more. A
        concurrent group delete too late for the not-found above to catch leaves
        those INSERTs violating ``fk_group_player_group_id_player_group`` -- the
        same :class:`GroupNotFoundError`, raised by a constraint rather than by
        the missing row (issue #2583). An interleaved second edit of the same
        group violates ``uq_group_player_group_uuid`` instead and raises
        :class:`GroupPlayerEditConflictError`: under READ COMMITTED the wholesale
        DELETE cannot see a player the winner committed after it ran, so the
        loser re-inserts a pair that now exists (issue #2613).
        """

    @abc.abstractmethod
    async def delete(self, group_id: GroupId) -> None:
        """Delete the group, its players, and its attachments (DATABASE.md cascade)."""

    @abc.abstractmethod
    async def attach(self, group_id: GroupId, server_id: ServerId) -> None:
        """Attach ``group_id`` to ``server_id``; the INSERT runs inside this call.

        Idempotent: an already-attached pair conflicts on ``pk_server_group``,
        which the adapter's ``ON CONFLICT DO NOTHING`` turns into a silent no-op
        (issue #2612).

        Not a staged write: the row's two foreign keys are enforced here rather
        than at the unit of work's commit, so a group or server deleted since the
        caller's pre-read raises the very error that pre-read raises, instead of
        surfacing at whatever the caller does next:
        :class:`GroupNotFoundError` for ``fk_server_group_group_id_player_group``
        and :class:`ServerNotFoundError` for ``fk_server_group_server_id_server``.
        """

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
