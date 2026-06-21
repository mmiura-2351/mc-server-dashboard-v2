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
from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditRecord
from mc_server_dashboard_api.audit.domain.name_resolver import NameResolver
from mc_server_dashboard_api.audit.domain.query import AuditFilter
from mc_server_dashboard_api.community.domain.value_objects import AuthUser, Permission
from mc_server_dashboard_api.dependencies import (
    get_audit_name_resolver,
    get_list_audit_log,
    require_permission,
    require_platform_admin,
)
from mc_server_dashboard_api.http_datetime import UtcDatetime

router = APIRouter()

# Pagination bounds: a sane page size, capped so a single query cannot scan the
# whole append-only trail (proportionate to NFR-SCALE-1).
_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50


# Target types whose ``target_id`` is a server UUID and so resolves to a server
# name. ``file`` targets carry the owning server's id (the audit convention), so
# they resolve the same way (issue #682).
_SERVER_TARGET_TYPES = frozenset({ops.TARGET_SERVER, ops.TARGET_FILE})


class AuditRecordResponse(BaseModel):
    """Public view of one audit-log row (DATABASE.md Section 9).

    ``actor_username``/``target_name``/``community_name`` are read-time display
    fields resolved from the live tables (issue #682); they are ``None`` when the
    soft-referenced subject was deleted or has no name source, in which case the
    client falls back to the raw id.
    """

    id: str
    operation: str
    outcome: str
    created_at: UtcDatetime
    actor_id: str | None
    actor_username: str | None
    community_id: str | None
    community_name: str | None
    target_type: str | None
    target_id: str | None
    target_name: str | None

    @classmethod
    def from_record(
        cls,
        record: AuditRecord,
        *,
        usernames: dict[uuid.UUID, str],
        server_names: dict[uuid.UUID, str],
        community_names: dict[uuid.UUID, str],
    ) -> "AuditRecordResponse":
        return cls(
            id=str(record.id),
            operation=record.operation,
            outcome=record.outcome.value,
            created_at=record.created_at,
            actor_id=str(record.actor_id) if record.actor_id is not None else None,
            actor_username=(
                usernames.get(record.actor_id) if record.actor_id is not None else None
            ),
            community_id=(
                str(record.community_id) if record.community_id is not None else None
            ),
            community_name=(
                community_names.get(record.community_id)
                if record.community_id is not None
                else None
            ),
            target_type=record.target_type,
            target_id=str(record.target_id) if record.target_id is not None else None,
            target_name=_target_name(record, usernames, server_names),
        )


def _target_name(
    record: AuditRecord,
    usernames: dict[uuid.UUID, str],
    server_names: dict[uuid.UUID, str],
) -> str | None:
    """Resolve a row's ``target_id`` to a display name by ``target_type``.

    ``user`` resolves to the username; ``server``/``file`` to the server name;
    every other type has no name source, so the result is ``None``.
    """
    if record.target_id is None:
        return None
    if record.target_type == ops.TARGET_USER:
        return usernames.get(record.target_id)
    if record.target_type in _SERVER_TARGET_TYPES:
        return server_names.get(record.target_id)
    return None


async def _enriched_responses(
    records: list[AuditRecord], resolver: NameResolver
) -> list[AuditRecordResponse]:
    """Resolve the page's ids to names in batched lookups, then build responses.

    Collects the distinct user/server/community ids across the page and resolves
    each kind in one call (``WHERE id IN (...)``) — not per row — then maps the
    results back onto each record (issue #682).
    """
    user_ids: set[uuid.UUID] = set()
    server_ids: set[uuid.UUID] = set()
    community_ids: set[uuid.UUID] = set()
    for record in records:
        if record.actor_id is not None:
            user_ids.add(record.actor_id)
        if record.community_id is not None:
            community_ids.add(record.community_id)
        if record.target_id is not None:
            if record.target_type == ops.TARGET_USER:
                user_ids.add(record.target_id)
            elif record.target_type in _SERVER_TARGET_TYPES:
                server_ids.add(record.target_id)

    usernames = await resolver.resolve_usernames(list(user_ids))
    server_names = await resolver.resolve_server_names(list(server_ids))
    community_names = await resolver.resolve_community_names(list(community_ids))

    return [
        AuditRecordResponse.from_record(
            record,
            usernames=usernames,
            server_names=server_names,
            community_names=community_names,
        )
        for record in records
    ]


class AuditLogResponse(BaseModel):
    records: list[AuditRecordResponse]


@router.get("/audit", dependencies=[Depends(require_platform_admin)])
async def list_audit_log(
    use_case: Annotated[ListAuditLog, Depends(get_list_audit_log)],
    resolver: Annotated[NameResolver, Depends(get_audit_name_resolver)],
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
    return AuditLogResponse(records=await _enriched_responses(records, resolver))


@router.get("/communities/{community_id}/audit")
async def list_community_audit_log(
    community_id: uuid.UUID,
    _authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("audit:read")))
    ],
    use_case: Annotated[ListAuditLog, Depends(get_list_audit_log)],
    resolver: Annotated[NameResolver, Depends(get_audit_name_resolver)],
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
    return AuditLogResponse(records=await _enriched_responses(records, resolver))
