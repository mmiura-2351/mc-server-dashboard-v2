"""Async-SQLAlchemy implementation of the ``PluginRepository`` Port.

Works on an ``AsyncSession`` owned by the enclosing ``UnitOfWork``; it stages
rows and runs reads but never commits -- commit is the unit of work's job.
Rows are translated to/from the framework-free domain entity here.
"""

from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mc_server_dashboard_api.servers.adapters.plugin_models import ServerPluginModel
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
)
from mc_server_dashboard_api.servers.domain.plugin_repository import PluginRepository
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


def _to_plugin(row: ServerPluginModel) -> ServerPlugin:
    return ServerPlugin(
        id=PluginId(row.id),
        server_id=ServerId(row.server_id),
        rel_path=row.rel_path,
        filename=row.filename,
        display_name=row.display_name,
        description=row.description,
        loader_type=LoaderType(row.loader_type),
        source=PluginSource(row.source),
        source_project_id=row.source_project_id,
        source_version_id=row.source_version_id,
        version_number=row.version_number,
        checksum_sha512=row.checksum_sha512,
        size_bytes=row.size_bytes,
        enabled=row.enabled,
        installed_by=row.installed_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyPluginRepository(PluginRepository):
    """:class:`PluginRepository` adapter over an ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, plugin: ServerPlugin) -> None:
        self._session.add(
            ServerPluginModel(
                id=plugin.id.value,
                server_id=plugin.server_id.value,
                rel_path=plugin.rel_path,
                filename=plugin.filename,
                display_name=plugin.display_name,
                description=plugin.description,
                loader_type=plugin.loader_type.value,
                source=plugin.source.value,
                source_project_id=plugin.source_project_id,
                source_version_id=plugin.source_version_id,
                version_number=plugin.version_number,
                checksum_sha512=plugin.checksum_sha512,
                size_bytes=plugin.size_bytes,
                enabled=plugin.enabled,
                installed_by=plugin.installed_by,
                created_at=plugin.created_at,
                updated_at=plugin.updated_at,
            )
        )

    async def get_by_id(
        self, server_id: ServerId, plugin_id: PluginId
    ) -> ServerPlugin | None:
        stmt = select(ServerPluginModel).where(
            ServerPluginModel.id == plugin_id.value,
            ServerPluginModel.server_id == server_id.value,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_plugin(row) if row is not None else None

    async def list_for_server(self, server_id: ServerId) -> list[ServerPlugin]:
        stmt = (
            select(ServerPluginModel)
            .where(ServerPluginModel.server_id == server_id.value)
            .order_by(ServerPluginModel.display_name, ServerPluginModel.id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_plugin(row) for row in rows]

    async def delete(self, plugin_id: PluginId) -> None:
        stmt = delete(ServerPluginModel).where(ServerPluginModel.id == plugin_id.value)
        await self._session.execute(stmt)

    async def get_by_rel_path(
        self, server_id: ServerId, rel_path: str
    ) -> ServerPlugin | None:
        stmt = select(ServerPluginModel).where(
            ServerPluginModel.server_id == server_id.value,
            ServerPluginModel.rel_path == rel_path,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_plugin(row) if row is not None else None

    async def update(self, plugin: ServerPlugin) -> None:
        stmt = (
            update(ServerPluginModel)
            .where(ServerPluginModel.id == plugin.id.value)
            .values(
                rel_path=plugin.rel_path,
                filename=plugin.filename,
                display_name=plugin.display_name,
                description=plugin.description,
                loader_type=plugin.loader_type.value,
                source=plugin.source.value,
                source_project_id=plugin.source_project_id,
                source_version_id=plugin.source_version_id,
                version_number=plugin.version_number,
                checksum_sha512=plugin.checksum_sha512,
                size_bytes=plugin.size_bytes,
                enabled=plugin.enabled,
                installed_by=plugin.installed_by,
                updated_at=plugin.updated_at,
            )
        )
        await self._session.execute(stmt)

    async def list_modrinth_plugins(self, server_id: ServerId) -> list[ServerPlugin]:
        stmt = (
            select(ServerPluginModel)
            .where(
                ServerPluginModel.server_id == server_id.value,
                ServerPluginModel.source == PluginSource.MODRINTH.value,
                ServerPluginModel.source_project_id.is_not(None),
            )
            .order_by(ServerPluginModel.display_name, ServerPluginModel.id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_plugin(row) for row in rows]
