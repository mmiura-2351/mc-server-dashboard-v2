"""Async-SQLAlchemy implementation of the ``ServerRepository`` Port.

Works on an ``AsyncSession`` owned by the enclosing ``UnitOfWork``; it stages
rows and runs reads but never commits — commit is the unit of work's job
(DATABASE.md Section 1). Rows are translated to/from the framework-free domain
entity here.
"""

from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mc_server_dashboard_api.servers.adapters.models import ServerModel
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.repositories import ServerRepository
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)


def _to_server(row: ServerModel) -> Server:
    return Server(
        id=ServerId(row.id),
        community_id=CommunityId(row.community_id),
        name=ServerName(row.name),
        mc_edition=row.mc_edition,
        mc_version=row.mc_version,
        server_type=ServerType(row.server_type),
        execution_backend=ExecutionBackend(row.execution_backend),
        config=dict(row.config),
        desired_state=DesiredState(row.desired_state),
        observed_state=ObservedState(row.observed_state),
        observed_at=row.observed_at,
        assigned_worker_id=(
            None if row.assigned_worker_id is None else WorkerId(row.assigned_worker_id)
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyServerRepository(ServerRepository):
    """:class:`ServerRepository` adapter over an ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, server: Server) -> None:
        self._session.add(
            ServerModel(
                id=server.id.value,
                community_id=server.community_id.value,
                name=server.name.value,
                mc_edition=server.mc_edition,
                mc_version=server.mc_version,
                server_type=server.server_type.value,
                execution_backend=server.execution_backend.value,
                config=server.config,
                desired_state=server.desired_state.value,
                observed_state=server.observed_state.value,
                observed_at=server.observed_at,
                assigned_worker_id=(
                    None
                    if server.assigned_worker_id is None
                    else server.assigned_worker_id.value
                ),
                created_at=server.created_at,
                updated_at=server.updated_at,
            )
        )

    async def get_by_id(self, server_id: ServerId) -> Server | None:
        row = await self._session.get(ServerModel, server_id.value)
        return _to_server(row) if row is not None else None

    async def get_by_community_and_name(
        self, community_id: CommunityId, name: ServerName
    ) -> Server | None:
        stmt = select(ServerModel).where(
            ServerModel.community_id == community_id.value,
            ServerModel.name == name.value,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_server(row) if row is not None else None

    async def list_for_community(self, community_id: CommunityId) -> list[Server]:
        stmt = select(ServerModel).where(ServerModel.community_id == community_id.value)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_server(row) for row in rows]

    async def update(self, server: Server) -> None:
        stmt = (
            update(ServerModel)
            .where(ServerModel.id == server.id.value)
            .values(
                name=server.name.value,
                config=server.config,
                updated_at=server.updated_at,
            )
        )
        await self._session.execute(stmt)

    async def delete(self, server_id: ServerId) -> None:
        stmt = delete(ServerModel).where(ServerModel.id == server_id.value)
        await self._session.execute(stmt)
