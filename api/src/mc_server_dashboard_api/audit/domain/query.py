"""The :class:`AuditQuery` Port and its filter value object (FR-AUD-3).

Reads the trail for the query endpoints: a platform-admin global view and a
Community-scoped view. The filter carries the optional narrowing predicates
(community, operation, actor, time range) and simple ``limit``/``offset``
pagination. ``community_id`` is set by the Community-scoped endpoint to bound the
result to one Community (the isolation the soft-referenced ``community_id`` index
backs, DATABASE.md Section 9); the platform view leaves it ``None``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Protocol

from mc_server_dashboard_api.audit.domain.events import AuditRecord


@dataclass(frozen=True)
class AuditFilter:
    """Narrowing predicates + pagination for an audit query (FR-AUD-3)."""

    community_id: uuid.UUID | None = None
    operation: str | None = None
    actor_id: uuid.UUID | None = None
    since: dt.datetime | None = None
    until: dt.datetime | None = None
    limit: int = 50
    offset: int = 0


class AuditQuery(Protocol):
    """Lists audit records matching a :class:`AuditFilter`, newest first."""

    async def list_records(self, filter: AuditFilter) -> list[AuditRecord]:
        """Return the matching records ordered by ``created_at`` descending."""
        ...
