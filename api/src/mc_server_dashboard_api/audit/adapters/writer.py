"""Async-SQLAlchemy adapter for the :class:`AuditWriter` Port (FR-AUD-2).

Each :meth:`write` opens its own short transaction from the session factory and
commits -- the trail is appended *after* and *independently of* the business
``UnitOfWork`` (fire-after-commit). It stamps a fresh id and the event time here.
A persistence failure is allowed to raise; the edge ``AuditRecorder`` catches it
so the broken trail never rolls back or fails the operation (must-not-raise).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.audit.adapters.models import AuditLogModel
from mc_server_dashboard_api.audit.domain.clock import Clock
from mc_server_dashboard_api.audit.domain.events import AuditEvent
from mc_server_dashboard_api.audit.domain.writer import AuditWriter


class SqlAlchemyAuditWriter(AuditWriter):
    """:class:`AuditWriter` adapter over an async-SQLAlchemy session factory."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def write(self, event: AuditEvent) -> None:
        async with self._session_factory() as session:
            session.add(
                AuditLogModel(
                    id=uuid.uuid4(),
                    actor_id=event.actor_id,
                    community_id=event.community_id,
                    operation=event.operation,
                    target_type=event.target_type,
                    target_id=event.target_id,
                    outcome=event.outcome.value,
                    created_at=self._clock.now(),
                )
            )
            await session.commit()
