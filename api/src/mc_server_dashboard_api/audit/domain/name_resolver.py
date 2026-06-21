"""The :class:`NameResolver` Port for audit read-time name enrichment (issue #682).

The audit trail stores only soft-referenced ids (DATABASE.md Section 9): the
``actor_id``/``target_id`` columns are plain UUIDs with no foreign keys, so the
row outlives the entities it describes. The query endpoints want to *display*
human-readable names alongside those ids without persisting them, so they resolve
the ids at read time against the live ``user``/``server``/``community`` tables.

This Port batches the lookup: the edge collects the distinct ids on the current
page and asks for them all at once (``WHERE id IN (...)``), avoiding per-row N+1.
A referenced subject may have been deleted; it is simply absent from the returned
mapping (the caller falls back to the raw id).
"""

from __future__ import annotations

import uuid
from typing import Protocol


class NameResolver(Protocol):
    """Batch-resolves audit-subject ids to display names; absent when deleted."""

    async def resolve_usernames(
        self, user_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """Return ``{user_id: username}`` for the ids that still exist."""
        ...

    async def resolve_server_names(
        self, server_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """Return ``{server_id: name}`` for the ids that still exist."""
        ...

    async def resolve_community_names(
        self, community_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """Return ``{community_id: name}`` for the ids that still exist."""
        ...
