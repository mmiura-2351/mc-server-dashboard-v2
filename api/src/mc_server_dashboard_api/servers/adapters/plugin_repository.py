"""Async-SQLAlchemy implementation of the ``PluginRepository`` Port.

Works on an ``AsyncSession`` owned by the enclosing ``UnitOfWork``; it stages
rows and runs reads but never commits -- commit is the unit of work's job.
Rows are translated to/from the framework-free domain entity here.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import cast

from sqlalchemy import delete, distinct, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mc_server_dashboard_api.servers.adapters.plugin_models import ServerPluginModel
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSide,
    PluginSource,
    ServerPlugin,
    has_enabled_geyser,
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
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        enabled=row.enabled,
        installed_by=row.installed_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        mod_identifier=row.mod_identifier,
        provides=row.provides or [],
        dependencies=row.dependencies or [],
        mc_versions=row.mc_versions or [],
        side=cast(PluginSide, row.side),
        catalog_dependencies=row.catalog_dependencies or [],
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
                sha256=plugin.sha256,
                size_bytes=plugin.size_bytes,
                enabled=plugin.enabled,
                installed_by=plugin.installed_by,
                created_at=plugin.created_at,
                updated_at=plugin.updated_at,
                mod_identifier=plugin.mod_identifier,
                provides=plugin.provides,
                dependencies=plugin.dependencies,
                mc_versions=plugin.mc_versions,
                side=plugin.side,
                catalog_dependencies=plugin.catalog_dependencies,
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

    async def enabled_geyser_server_ids(
        self, server_ids: Iterable[ServerId]
    ) -> set[ServerId]:
        ids = [server_id.value for server_id in server_ids]
        if not ids:
            return set()
        # One query for every server in ``ids`` (issue #1555), rather than a
        # list_for_server() per server, so the servers list response gate does not
        # add a per-row query. Rows are grouped by server_id and classified with
        # the same has_enabled_geyser() predicate list_for_server() callers use.
        stmt = select(ServerPluginModel).where(ServerPluginModel.server_id.in_(ids))
        rows = (await self._session.execute(stmt)).scalars().all()
        grouped: dict[uuid.UUID, list[ServerPlugin]] = {}
        for row in rows:
            grouped.setdefault(row.server_id, []).append(_to_plugin(row))
        return {
            ServerId(server_id)
            for server_id, plugins in grouped.items()
            if has_enabled_geyser(plugins)
        }

    async def delete(self, plugin_id: PluginId) -> None:
        stmt = delete(ServerPluginModel).where(ServerPluginModel.id == plugin_id.value)
        await self._session.execute(stmt)

    async def get_by_rel_path(
        self, server_id: ServerId, rel_path: str
    ) -> ServerPlugin | None:
        # Normalize the .disabled suffix so a clean path and its disabled variant
        # share the same per-server slot (issue #1316): a disabled plugin still
        # occupies its base filename and must block a same-named install. Prefer an
        # exact-path match so the collision guards see the row actually at the
        # queried path rather than its (self-excluded) suffix sibling.
        clean = rel_path.removesuffix(".disabled")
        stmt = select(ServerPluginModel).where(
            ServerPluginModel.server_id == server_id.value,
            ServerPluginModel.rel_path.in_((clean, f"{clean}.disabled")),
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        row = next((r for r in rows if r.rel_path == rel_path), None)
        if row is None:
            row = next(iter(rows), None)
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
                sha256=plugin.sha256,
                size_bytes=plugin.size_bytes,
                enabled=plugin.enabled,
                installed_by=plugin.installed_by,
                updated_at=plugin.updated_at,
                mod_identifier=plugin.mod_identifier,
                provides=plugin.provides,
                dependencies=plugin.dependencies,
                mc_versions=plugin.mc_versions,
                side=plugin.side,
                catalog_dependencies=plugin.catalog_dependencies,
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

    async def get_by_source_project_id(
        self, server_id: ServerId, source_project_id: str
    ) -> ServerPlugin | None:
        stmt = select(ServerPluginModel).where(
            ServerPluginModel.server_id == server_id.value,
            ServerPluginModel.source_project_id == source_project_id,
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return _to_plugin(row) if row is not None else None

    async def all_sha256s(self) -> set[str]:
        stmt = select(distinct(ServerPluginModel.sha256)).where(
            ServerPluginModel.sha256.is_not(None)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        # The WHERE clause guarantees no None values; the cast satisfies mypy.
        return {r for r in rows if r is not None}

    async def find_sha256_by_sha512(self, checksum_sha512: str) -> str | None:
        stmt = (
            select(ServerPluginModel.sha256)
            .where(
                ServerPluginModel.checksum_sha512 == checksum_sha512,
                ServerPluginModel.sha256.is_not(None),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
