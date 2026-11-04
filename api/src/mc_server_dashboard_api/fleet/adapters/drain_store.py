"""SQLAlchemy adapter for the fleet ``WorkerDrainStore`` Port (issue #3535).

Each call runs in its own session and commits before it returns. The fleet worker
id is an opaque string, but registration admits only UUIDs (CONFIGURATION.md
Section 6.1 ``worker.id``), and the drain use case answers an unregistered id
before it reaches this store, so the id is stored as a UUID like
``server.assigned_worker_id``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.fleet.adapters.models import WorkerDrainModel
from mc_server_dashboard_api.fleet.domain.clock import Clock
from mc_server_dashboard_api.fleet.domain.drain_store import WorkerDrainStore
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId


class SqlAlchemyWorkerDrainStore(WorkerDrainStore):
    """:class:`WorkerDrainStore` over an async-SQLAlchemy session factory."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def drain(self, worker_id: WorkerId) -> None:
        # A re-drain keeps the first drained_at: the Worker has been draining since.
        stmt = (
            insert(WorkerDrainModel)
            .values(worker_id=uuid.UUID(worker_id.value), drained_at=self._clock.now())
            .on_conflict_do_nothing(index_elements=[WorkerDrainModel.worker_id])
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()

    async def undrain(self, worker_id: WorkerId) -> None:
        stmt = delete(WorkerDrainModel).where(
            WorkerDrainModel.worker_id == uuid.UUID(worker_id.value)
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()

    async def drained(self) -> frozenset[WorkerId]:
        async with self._session_factory() as session:
            rows = await session.scalars(select(WorkerDrainModel.worker_id))
            return frozenset(WorkerId(str(worker_id)) for worker_id in rows)
