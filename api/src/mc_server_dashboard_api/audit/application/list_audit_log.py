"""The ``ListAuditLog`` use case: read the audit trail for the query endpoints.

A thin read use case behind the platform-admin and Community-scoped query
endpoints (FR-AUD-3). It holds only the :class:`AuditQuery` Port, so the HTTP edge
and the query adapter stay decoupled. Scoping (which Community a member may see)
and permission gating are the edge's job; this use case applies the filter as
given.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.audit.domain.events import AuditRecord
from mc_server_dashboard_api.audit.domain.query import AuditFilter, AuditQuery


@dataclass(frozen=True)
class ListAuditLog:
    query: AuditQuery

    async def __call__(self, filter: AuditFilter) -> list[AuditRecord]:
        return await self.query.list_records(filter)
