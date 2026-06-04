"""Async-SQLAlchemy adapter for the :class:`AuditQuery` Port (FR-AUD-3).

Reads the ``audit_log`` table for the query endpoints, applying the optional
narrowing predicates and ``limit``/``offset`` pagination, newest first. Read-only:
opens its own session and never writes.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.audit.adapters.models import AuditLogModel
from mc_server_dashboard_api.audit.domain.events import AuditRecord, Outcome
from mc_server_dashboard_api.audit.domain.query import AuditFilter, AuditQuery


def _to_record(row: AuditLogModel) -> AuditRecord:
    return AuditRecord(
        id=row.id,
        operation=row.operation,
        outcome=Outcome(row.outcome),
        created_at=row.created_at,
        actor_id=row.actor_id,
        community_id=row.community_id,
        target_type=row.target_type,
        target_id=row.target_id,
    )


class SqlAlchemyAuditQuery(AuditQuery):
    """:class:`AuditQuery` adapter over an async-SQLAlchemy session factory."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_records(self, filter: AuditFilter) -> list[AuditRecord]:
        stmt = select(AuditLogModel)
        if filter.community_id is not None:
            stmt = stmt.where(AuditLogModel.community_id == filter.community_id)
        if filter.operation is not None:
            stmt = stmt.where(AuditLogModel.operation == filter.operation)
        if filter.actor_id is not None:
            stmt = stmt.where(AuditLogModel.actor_id == filter.actor_id)
        if filter.since is not None:
            stmt = stmt.where(AuditLogModel.created_at >= filter.since)
        if filter.until is not None:
            stmt = stmt.where(AuditLogModel.created_at < filter.until)
        stmt = (
            stmt.order_by(AuditLogModel.created_at.desc())
            .limit(filter.limit)
            .offset(filter.offset)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_record(row) for row in rows]
