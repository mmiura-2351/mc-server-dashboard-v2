"""Async-SQLAlchemy implementation of the ``ResourcePackRepository`` Port.

Works on an ``AsyncSession`` owned by the enclosing ``UnitOfWork``; it stages
rows and runs reads but never commits -- commit is the unit of work's job.
Rows are translated to/from the framework-free domain entity here.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mc_server_dashboard_api.servers.adapters.resource_pack_models import (
    ResourcePackModel,
    ServerResourcePackAssignmentModel,
)
from mc_server_dashboard_api.servers.domain.resource_pack import (
    ResourcePack,
    ResourcePackAssignment,
    ResourcePackId,
)
from mc_server_dashboard_api.servers.domain.resource_pack_repository import (
    ResourcePackRepository,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


def _to_pack(row: ResourcePackModel) -> ResourcePack:
    return ResourcePack(
        id=ResourcePackId(row.id),
        filename=row.filename,
        display_name=row.display_name,
        description=row.description,
        sha1_hash=row.sha1_hash,
        sha256_hash=row.sha256_hash,
        size_bytes=row.size_bytes,
        uploaded_by=row.uploaded_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_assignment(row: ServerResourcePackAssignmentModel) -> ResourcePackAssignment:
    return ResourcePackAssignment(
        server_id=ServerId(row.server_id),
        resource_pack_id=ResourcePackId(row.resource_pack_id),
        require_resource_pack=row.require_resource_pack,
        resource_pack_prompt=row.resource_pack_prompt,
        assigned_by=row.assigned_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyResourcePackRepository(ResourcePackRepository):
    """:class:`ResourcePackRepository` adapter over an ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, pack: ResourcePack) -> None:
        self._session.add(
            ResourcePackModel(
                id=pack.id.value,
                filename=pack.filename,
                display_name=pack.display_name,
                description=pack.description,
                sha1_hash=pack.sha1_hash,
                sha256_hash=pack.sha256_hash,
                size_bytes=pack.size_bytes,
                uploaded_by=pack.uploaded_by,
                created_at=pack.created_at,
                updated_at=pack.updated_at,
            )
        )

    async def get_by_id(self, pack_id: ResourcePackId) -> ResourcePack | None:
        stmt = select(ResourcePackModel).where(
            ResourcePackModel.id == pack_id.value,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_pack(row) if row is not None else None

    async def list_all(self) -> list[ResourcePack]:
        stmt = select(ResourcePackModel).order_by(
            ResourcePackModel.display_name, ResourcePackModel.id
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_pack(row) for row in rows]

    async def delete(self, pack_id: ResourcePackId) -> None:
        stmt = delete(ResourcePackModel).where(
            ResourcePackModel.id == pack_id.value,
        )
        await self._session.execute(stmt)

    async def add_assignment(self, assignment: ResourcePackAssignment) -> None:
        self._session.add(
            ServerResourcePackAssignmentModel(
                server_id=assignment.server_id.value,
                resource_pack_id=assignment.resource_pack_id.value,
                require_resource_pack=assignment.require_resource_pack,
                resource_pack_prompt=assignment.resource_pack_prompt,
                assigned_by=assignment.assigned_by,
                created_at=assignment.created_at,
                updated_at=assignment.updated_at,
            )
        )

    async def get_assignment_by_server(
        self, server_id: ServerId
    ) -> ResourcePackAssignment | None:
        stmt = select(ServerResourcePackAssignmentModel).where(
            ServerResourcePackAssignmentModel.server_id == server_id.value,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_assignment(row) if row is not None else None

    async def delete_assignment(self, server_id: ServerId) -> None:
        stmt = delete(ServerResourcePackAssignmentModel).where(
            ServerResourcePackAssignmentModel.server_id == server_id.value,
        )
        await self._session.execute(stmt)

    async def list_assignments_for_pack(
        self, pack_id: ResourcePackId
    ) -> list[ResourcePackAssignment]:
        stmt = select(ServerResourcePackAssignmentModel).where(
            ServerResourcePackAssignmentModel.resource_pack_id == pack_id.value,
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_assignment(row) for row in rows]
