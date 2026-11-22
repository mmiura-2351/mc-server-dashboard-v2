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
    PendingGroupSync,
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
        (``uq_player_group_community_kind_name``, issue #2000). The same flush
        enforces ``fk_player_group_community_id_community``: a community deleted
        between the request's authorization gate and this INSERT raises
        :class:`CommunityNotFoundError`, the same not-found that gate answers for
        a community that is gone (issue #2924).
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

        **Lock order (issue #2965).** The re-read locks the group's row (FOR NO
        KEY UPDATE) before any ``group_player`` row is touched -- the order a group
        delete takes them in: the row (:meth:`lock_against_attach`), then the
        player rows its cascade removes. A save and a delete of one group therefore
        never wait on each other in a cycle; the later one waits for the earlier to
        commit. A delete behind a save deletes once the save has committed; a save
        behind a delete finds the row gone and raises the not-found above. A second
        save of the group waits the same way; its DELETE then sees, and replaces,
        the rows the first one wrote, so the later save's player set wins -- which
        is why an edit reads the group under :meth:`lock_for_edit` or
        :meth:`lock_against_attach` before it saves. An attach's FK check does not
        conflict with the lock.

        That lock closes three later sites that used to answer a racer. A
        concurrent group delete too late for the not-found above to catch left the
        replacement rows' INSERTs violating
        ``fk_group_player_group_id_player_group`` -- the same
        :class:`GroupNotFoundError`, raised by a constraint rather than by the
        missing row (issue #2583) -- or, for a rename, left the pending name
        UPDATE matching zero rows, which the ORM reports as ``StaleDataError``
        and the adapter raises as the same not-found (issue #2937). An interleaved
        second edit of the same group violated ``uq_group_player_group_uuid``,
        raised as :class:`GroupPlayerEditConflictError`: under READ COMMITTED the
        wholesale DELETE could not see a player the winner committed after it ran,
        so the loser re-inserted a pair that then existed (issue #2613). Each of
        those racers now waits on the lock instead; the adapter's translations of
        all three are still in place.
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
    async def lock_against_attach(self, group_id: GroupId) -> None:
        """Hold off every attach of ``group_id`` until this transaction ends.

        A change that lists the group's servers in order to mark them
        (:meth:`mark_sync_pending`) takes this first, so the list cannot go stale
        under it (issue #3223): an attach that committed earlier is in the list,
        and a later one waits for the change — then sees it, or finds the group
        deleted. A group that does not exist locks nothing.

        It holds off every edit and delete of the group as well, as
        :meth:`lock_for_edit` does, so a player edit takes only this one.
        """

    @abc.abstractmethod
    async def lock_for_edit(self, group_id: GroupId) -> None:
        """Hold off every other edit and delete of ``group_id`` until this ends.

        An edit takes this before it reads the group (issue #2965). :meth:`save`
        writes back the whole group it is given -- its name and its full player
        set -- so an edit that read the group before another edit committed would
        write back what it read and undo that edit with no error. Under the lock,
        an edit behind another reads what that one committed, and an edit behind
        a delete finds the group gone. Attaches are not held off. A group that
        does not exist locks nothing.
        """

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

    @abc.abstractmethod
    async def mark_sync_pending(
        self, server_ids: list[ServerId], kind: GroupKind
    ) -> None:
        """Record that each server's ``kind`` file is owed a regeneration (#3223).

        One mark per ``(server, kind)``; marking again keeps the single mark and
        gives it a fresh token, so :meth:`clear_sync_pending` called with a token
        read earlier leaves it standing. A server that no longer exists is skipped
        rather than refused: its file went with it.
        """

    @abc.abstractmethod
    async def list_sync_pending(self, server_id: ServerId) -> list[PendingGroupSync]:
        """Return the regenerations ``server_id`` is owed, ordered by kind."""

    @abc.abstractmethod
    async def clear_sync_pending(
        self, server_id: ServerId, applied: list[PendingGroupSync]
    ) -> None:
        """Clear the marks in ``applied`` that still carry the token it names.

        A mark recorded again since ``applied`` was read is left in place: the
        change behind it may not be in the file the caller regenerated.
        """
