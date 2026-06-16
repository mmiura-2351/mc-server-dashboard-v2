"""Catalog integration use cases (issue #1151).

All use cases load the server to derive loader + game-version facets from its
metadata. InstallFromCatalog follows the same at-rest gate, lifecycle-lock, and
FileStore write pattern as :class:`InstallPlugin`.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogProject,
    CatalogProvider,
    CatalogSearchResponse,
    CatalogVersion,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    CatalogChecksumMismatchError,
    CatalogProjectNotFoundError,
    PluginAlreadyExistsError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileStore
from mc_server_dashboard_api.servers.domain.lifecycle_lock import (
    LifecycleLock,
    NullLifecycleLock,
)
from mc_server_dashboard_api.servers.domain.plugin import (
    PluginId,
    PluginSource,
    ServerPlugin,
    content_dir_for_server_type,
    loader_type_for_server_type,
    modrinth_loader_for_server_type,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId


async def _load(
    uow: UnitOfWork, community_id: CommunityId, server_id: ServerId
) -> Server:
    server = await uow.servers.get_by_id(server_id)
    if server is None or server.community_id != community_id:
        raise ServerNotFoundError(str(server_id.value))
    return server


@dataclass(frozen=True)
class SearchCatalog:
    """Search the external catalog with auto-applied loader + game-version facets."""

    uow: UnitOfWork
    catalog: CatalogProvider

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        query: str,
        limit: int = 20,
        offset: int = 0,
    ) -> CatalogSearchResponse:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
            loader = modrinth_loader_for_server_type(server.server_type)
        return await self.catalog.search(
            query=query,
            loader=loader,
            game_versions=[server.mc_version],
            limit=limit,
            offset=offset,
        )


@dataclass(frozen=True)
class GetCatalogProject:
    """Fetch project detail + compatible versions from the catalog."""

    uow: UnitOfWork
    catalog: CatalogProvider

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        project_id_or_slug: str,
    ) -> tuple[CatalogProject, list[CatalogVersion]]:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
            loader = modrinth_loader_for_server_type(server.server_type)
        project = await self.catalog.get_project(project_id_or_slug)
        versions = await self.catalog.list_versions(
            project_id_or_slug,
            loader=loader,
            game_versions=[server.mc_version],
        )
        return project, versions


@dataclass(frozen=True)
class InstallFromCatalog:
    """Download a catalog version and install it into the server's content dir.

    Follows the same at-rest gate, lifecycle-lock, entity-creation, and
    UoW-commit pattern as :class:`InstallPlugin`.
    """

    uow: UnitOfWork
    catalog: CatalogProvider
    file_store: FileStore
    clock: Clock
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        project_id: str,
        version_id: str,
        installed_by: uuid.UUID | None = None,
    ) -> ServerPlugin:
        # Fetch version metadata to find the requested version.
        versions = await self.catalog.list_versions(project_id)
        version = next((v for v in versions if v.version_id == version_id), None)
        if version is None:
            raise CatalogProjectNotFoundError(
                f"version {version_id} not found for project {project_id}"
            )

        # Select primary file (fallback to first).
        primary = next((f for f in version.files if f.primary), None)
        if primary is None and version.files:
            primary = version.files[0]
        if primary is None:
            raise CatalogProjectNotFoundError(f"no files in version {version_id}")
        file = primary

        # Fetch project metadata for display_name/description.
        project = await self.catalog.get_project(project_id)

        async with self.lifecycle_lock.hold(server_id):
            async with self.uow:
                server = await _load(self.uow, community_id, server_id)
                if not server.is_at_rest():
                    raise ServerFilesUnsettledError(str(server_id.value))

                content_dir = content_dir_for_server_type(server.server_type)
                loader_type = loader_type_for_server_type(server.server_type)
                rel_path = f"{content_dir}/{file.filename}"
                self.file_store.validate_rel_path(rel_path)

                existing = await self.uow.plugins.get_by_rel_path(server_id, rel_path)
                if existing is not None:
                    raise PluginAlreadyExistsError(rel_path)

                # Download and verify checksum.
                content = await self.catalog.download_file(file.url)
                computed_hash = hashlib.sha512(content).hexdigest()
                if file.sha512 and computed_hash != file.sha512:
                    raise CatalogChecksumMismatchError(
                        f"expected {file.sha512}, got {computed_hash}"
                    )

                await self.file_store.write_file(
                    community_id=community_id,
                    server_id=server_id,
                    rel_path=rel_path,
                    content=content,
                )

                now = self.clock.now()
                plugin = ServerPlugin(
                    id=PluginId.new(),
                    server_id=server_id,
                    rel_path=rel_path,
                    filename=file.filename,
                    display_name=project.title,
                    description=project.description,
                    loader_type=loader_type,
                    source=PluginSource.MODRINTH,
                    source_project_id=project_id,
                    source_version_id=version_id,
                    version_number=version.version_number,
                    checksum_sha512=computed_hash,
                    size_bytes=len(content),
                    enabled=True,
                    installed_by=installed_by,
                    created_at=now,
                    updated_at=now,
                )
                await self.uow.plugins.add(plugin)
                await self.uow.commit()
                return plugin
