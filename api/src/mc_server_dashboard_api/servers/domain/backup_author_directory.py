"""The ``BackupAuthorDirectory`` Port: resolving a backup author id to a username.

A backup's ``created_by`` is a soft reference to a user id (no FK, so the row
survives the actor's deletion; DATABASE.md Section 8). The Backups read model
shows *who* made a backup, which needs a human-readable username rather than the
raw id. This Port is that seam: it resolves backup author ids to display
usernames for the listing (issue #688). It is implemented at the edge (adapters)
against the identity user store, so the servers context stays unaware of how
users are stored. Scoped to the backups read path; not a shared name-resolution
abstraction.
"""

from __future__ import annotations

import abc
import uuid


class BackupAuthorDirectory(abc.ABC):
    """Port: resolve backup author ids to display usernames."""

    @abc.abstractmethod
    async def usernames_for(self, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Resolve ``user_ids`` to their display usernames in one batch lookup.

        Returns a mapping for the ids that resolve; ids absent from the identity
        store (a deleted author) are simply omitted from the result, so callers
        fall back to showing the raw id. Implementations must answer in a single
        indexed query, never one lookup per id.
        """
