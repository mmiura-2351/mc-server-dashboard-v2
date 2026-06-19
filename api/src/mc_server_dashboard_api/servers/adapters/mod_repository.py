"""Async-SQLAlchemy implementation of the ``ModRepository`` Port.

Works on an ``AsyncSession`` owned by the enclosing ``UnitOfWork``; it stages
rows and runs reads but never commits -- commit is the unit of work's job.
Rows are translated to/from the framework-free domain entity here.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mc_server_dashboard_api.servers.adapters.mod_models import ModModel
from mc_server_dashboard_api.servers.domain.mod import (
    Mod,
    ModId,
    ModLoader,
    ModSide,
)
from mc_server_dashboard_api.servers.domain.mod_repository import ModRepository


def _to_mod(row: ModModel) -> Mod:
    return Mod(
        id=ModId(row.id),
        filename=row.filename,
        display_name=row.display_name,
        description=row.description,
        loader_type=row.loader_type,  # type: ignore[arg-type]
        mod_identifier=row.mod_identifier,
        provides=row.provides,
        version_number=row.version_number,
        mc_versions=row.mc_versions,
        side=row.side,  # type: ignore[arg-type]
        dependencies=row.dependencies,
        sha256_hash=row.sha256_hash,
        sha512_hash=row.sha512_hash,
        size_bytes=row.size_bytes,
        source=row.source,  # type: ignore[arg-type]
        source_project_id=row.source_project_id,
        source_version_id=row.source_version_id,
        uploaded_by=row.uploaded_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyModRepository(ModRepository):
    """:class:`ModRepository` adapter over an ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, mod: Mod) -> None:
        self._session.add(
            ModModel(
                id=mod.id.value,
                filename=mod.filename,
                display_name=mod.display_name,
                description=mod.description,
                loader_type=mod.loader_type,
                mod_identifier=mod.mod_identifier,
                provides=mod.provides,
                version_number=mod.version_number,
                mc_versions=mod.mc_versions,
                side=mod.side,
                dependencies=mod.dependencies,
                sha256_hash=mod.sha256_hash,
                sha512_hash=mod.sha512_hash,
                size_bytes=mod.size_bytes,
                source=mod.source,
                source_project_id=mod.source_project_id,
                source_version_id=mod.source_version_id,
                uploaded_by=mod.uploaded_by,
                created_at=mod.created_at,
                updated_at=mod.updated_at,
            )
        )

    async def get_by_id(self, mod_id: ModId) -> Mod | None:
        stmt = select(ModModel).where(ModModel.id == mod_id.value)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_mod(row) if row is not None else None

    async def get_by_sha256(self, sha256_hash: str) -> Mod | None:
        stmt = select(ModModel).where(ModModel.sha256_hash == sha256_hash)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_mod(row) if row is not None else None

    async def list_all(
        self,
        *,
        loader_type: ModLoader | None = None,
        mc_version: str | None = None,
        side: ModSide | None = None,
    ) -> list[Mod]:
        stmt = select(ModModel)
        if loader_type is not None:
            stmt = stmt.where(ModModel.loader_type == loader_type)
        if side is not None:
            stmt = stmt.where(ModModel.side == side)
        if mc_version is not None:
            stmt = stmt.where(ModModel.mc_versions.contains([mc_version]))
        stmt = stmt.order_by(ModModel.display_name, ModModel.id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_mod(row) for row in rows]

    async def delete(self, mod_id: ModId) -> None:
        stmt = delete(ModModel).where(ModModel.id == mod_id.value)
        await self._session.execute(stmt)
