"""The audit-log query surface (FR-AUD-3).

Two read endpoints over the :class:`ListAuditLog` use case:

- ``GET /audit`` -- the platform-admin global view, guarded by the platform-admin
  axis (``require_platform_admin``, FR-AUTHZ-5). It may filter by ``community``,
  ``operation``, ``actor``, and a ``since``/``until`` time range.
- ``GET /communities/{community_id}/audit`` -- the Community-scoped view, gated by
  ``audit:read`` through the two-layer check (non-member -> 404 with no existence
  signal, Layer-1 posture; member without the permission -> 403). The query is
  forced to the path Community, so a member can never read another Community's
  trail regardless of the ``operation``/``actor`` filters they pass.

Both support simple ``limit``/``offset`` pagination. Results are newest-first.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from mc_server_dashboard_api.audit.application.list_audit_log import ListAuditLog
from mc_server_dashboard_api.audit.domain.events import AuditRecord
from mc_server_dashboard_api.audit.domain.query import AuditFilter
from mc_server_dashboard_api.community.domain.value_objects import AuthUser, Permission
from mc_server_dashboard_api.dependencies import (
    get_list_audit_log,
    require_permission,
    require_platform_admin,
)

router = APIRouter()

# Pagination bounds: a sane page size, capped so a single query cannot scan the
# whole append-only trail (proportionate to NFR-SCALE-1).
_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50


class AuditRecordResponse(BaseModel):
    """Public view of one audit-log row (DATABASE.md Section 9)."""

    id: str
    operation: str
    outcome: str
    created_at: dt.datetime
    actor_id: str | None
    community_id: str | None
    target_type: str | None
    target_id: str | None

    @classmethod
    def from_record(cls, record: AuditRecord) -> "AuditRecordResponse":
        return cls(
            id=str(record.id),
            operation=record.operation,
            outcome=record.outcome.value,
            created_at=record.created_at,
            actor_id=str(record.actor_id) if record.actor_id is not None else None,
            community_id=(
                str(record.community_id) if record.community_id is not None else None
            ),
            target_type=record.target_type,
            target_id=str(record.target_id) if record.target_id is not None else None,
        )


class AuditLogResponse(BaseModel):
    records: list[AuditRecordResponse]


@router.get("/audit", dependencies=[Depends(require_platform_admin)])
async def list_audit_log(
    use_case: Annotated[ListAuditLog, Depends(get_list_audit_log)],
    community: uuid.UUID | None = None,
    operation: str | None = None,
    actor: uuid.UUID | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> AuditLogResponse:
    records = await use_case(
        AuditFilter(
            community_id=community,
            operation=operation,
            actor_id=actor,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
    )
    return AuditLogResponse(
        records=[AuditRecordResponse.from_record(r) for r in records]
    )


@router.get("/communities/{community_id}/audit")
async def list_community_audit_log(
    community_id: uuid.UUID,
    _authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("audit:read")))
    ],
    use_case: Annotated[ListAuditLog, Depends(get_list_audit_log)],
    operation: str | None = None,
    actor: uuid.UUID | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> AuditLogResponse:
    records = await use_case(
        AuditFilter(
            # Forced to the path Community: a member cannot read another's trail.
            community_id=community_id,
            operation=operation,
            actor_id=actor,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
    )
    return AuditLogResponse(
        records=[AuditRecordResponse.from_record(r) for r in records]
    )
