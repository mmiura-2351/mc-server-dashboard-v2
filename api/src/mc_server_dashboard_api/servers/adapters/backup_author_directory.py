"""Adapter implementing the servers :class:`BackupAuthorDirectory` over identity.

This is the edge where the backups read model's "who made this backup?" question
is answered against the identity user store (issue #688). Only the *adapter*
crosses contexts — the servers domain stays unaware of identity (DATABASE.md
Section 5) — and it asks only for usernames, in one batched indexed query.
"""

from __future__ import annotations

import uuid

from mc_server_dashboard_api.identity.domain.unit_of_work import (
    UnitOfWork as IdentityUnitOfWork,
)
from mc_server_dashboard_api.identity.domain.value_objects import (
    UserId as IdentityUserId,
)
from mc_server_dashboard_api.servers.domain.backup_author_directory import (
    BackupAuthorDirectory,
)


class IdentityBackupAuthorDirectory(BackupAuthorDirectory):
    """:class:`BackupAuthorDirectory` backed by the identity user repository."""

    def __init__(self, uow: IdentityUnitOfWork) -> None:
        self._uow = uow

    async def usernames_for(self, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not user_ids:
            return {}
        async with self._uow as uow:
            resolved = await uow.users.usernames_by_id(
                [IdentityUserId(uid) for uid in user_ids]
            )
        return {
            identity_id.value: username.value
            for identity_id, username in resolved.items()
        }
