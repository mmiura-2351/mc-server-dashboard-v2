"""Catalog integration use cases (issue #1151).

All use cases load the server to derive loader + game-version facets from its
metadata. InstallFromCatalog follows the same at-rest gate, lifecycle-lock, and
FileStore write pattern as :class:`InstallPlugin`.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.servers.application.plugin_cache import (
    ingest_into_cache,
)
from mc_server_dashboard_api.servers.application.plugin_manifest import (
    parse_manifest_at_ingest,
)
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogDependency,
    CatalogFile,
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
    CatalogUnavailableError,
    InvalidFilePathError,
    PluginAlreadyExistsError,
    PluginNotFoundError,
    ServerFileNotFoundError,
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
    PluginSide,
    PluginSource,
    ServerPlugin,
    content_dir_for_server_type,
    loader_type_for_server_type,
    modrinth_loader_for_server_type,
    working_set_path,
    working_set_present,
)
from mc_server_dashboard_api.servers.domain.plugin_cache_store import PluginCacheStore
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
    ServerType,
)

# Modrinth's per-catalog-version dependency classifications we capture: required
# (issue #1321, drives validation/resolution) and incompatible (issue #1318,
# blocks resolution). The other types (optional/embedded) are not stored.
_REQUIRED_DEP_TYPE = "required"
_INCOMPATIBLE_DEP_TYPE = "incompatible"


async def _load(
    uow: UnitOfWork, community_id: CommunityId, server_id: ServerId
) -> Server:
    server = await uow.servers.get_by_id(server_id)
    if server is None or server.community_id != community_id:
        raise ServerNotFoundError(str(server_id.value))
    return server


_CATALOG_DEPS_CONCURRENCY = 5  # Max parallel dep-project lookups at ingest.


async def capture_catalog_dependencies(
    catalog: CatalogProvider, version: CatalogVersion
) -> list[dict[str, object]]:
    """Capture a version's REQUIRED and INCOMPATIBLE catalog deps (issues #1321, #1318).

    Returns the persisted shape, keyed by ``project_id``, for each ``required`` and
    each ``incompatible`` catalog dependency edge of ``version``. A required edge is
    stored as ``{"project_id", "required": True, "slug", "title"}`` (drives missing-
    dep validation and resolution); an incompatible edge as ``{"project_id",
    "incompatible": True, "slug", "title"}`` (blocks resolution when the target is
    present, issue #1318). Each dep project's ``slug`` / ``title`` is fetched so the
    WebUI can render a human label without an extra Modrinth round-trip later; a
    failed/unavailable lookup leaves them ``None`` (best-effort, never aborts the
    install). Optional/embedded edges are not stored.
    """

    captured = [
        dep
        for dep in version.dependencies
        if dep.dependency_type in (_REQUIRED_DEP_TYPE, _INCOMPATIBLE_DEP_TYPE)
        and dep.project_id
    ]
    sem = asyncio.Semaphore(_CATALOG_DEPS_CONCURRENCY)

    async def _label(dep: CatalogDependency) -> dict[str, object]:
        slug: str | None = None
        title: str | None = None
        async with sem:
            try:
                proj = await catalog.get_project(dep.project_id)
                slug = proj.slug
                title = proj.title
            except (CatalogUnavailableError, CatalogProjectNotFoundError):
                pass
        kind = (
            "required" if dep.dependency_type == _REQUIRED_DEP_TYPE else "incompatible"
        )
        return {
            "project_id": dep.project_id,
            kind: True,
            "slug": slug,
            "title": title,
        }

    return list(await asyncio.gather(*(_label(dep) for dep in captured)))


def side_from_modrinth(client_side: str, server_side: str) -> PluginSide:
    """Map Modrinth ``client_side`` / ``server_side`` to a :data:`PluginSide`.

    Modrinth declares each environment as ``required`` / ``optional`` /
    ``unsupported`` / ``unknown``. Only an explicit ``unsupported`` on one side
    narrows the result: server-unsupported -> ``client``, client-unsupported ->
    ``server``. Anything else (the catalog's most common case, and any
    ``unknown``) is ``both`` -- the safe default that is present everywhere
    (issue #1308).
    """

    if server_side == "unsupported" and client_side != "unsupported":
        return "client"
    if client_side == "unsupported" and server_side != "unsupported":
        return "server"
    return "both"


async def _resolve_modrinth_content(
    *,
    uow: UnitOfWork,
    catalog: CatalogProvider,
    cache: PluginCacheStore,
    file: CatalogFile,
) -> tuple[bytes, str]:
    """Return ``(jar bytes, sha256)`` for a Modrinth ``file``, using the cache.

    Download cache (issue #1306): if a prior install recorded this version's
    published SHA-512 against a cached SHA-256 whose blob still exists, the bytes
    are served from the cache and the HTTP download is skipped. Otherwise the jar
    is downloaded, its SHA-512 verified against the catalog hash, its SHA-256
    computed, and the blob stored once (dedup-on-ingest).
    """

    if not file.sha512:
        raise CatalogChecksumMismatchError("no sha512 hash provided by catalog")

    async with uow:
        cached_sha256 = await uow.plugins.find_sha256_by_sha512(file.sha512)
    if cached_sha256 is not None and await cache.has(cached_sha256):
        content = b"".join([chunk async for chunk in cache.open(cached_sha256)])
        # Re-verify integrity: the blob may have been corrupted or tampered
        # with in object storage since it was originally cached (issue #1402).
        if hashlib.sha512(content).hexdigest() != file.sha512:
            raise CatalogChecksumMismatchError(
                f"cached blob {cached_sha256} failed SHA-512 re-verification"
            )
        return content, cached_sha256

    content = await catalog.download_file(file.url)
    computed_hash = hashlib.sha512(content).hexdigest()
    if computed_hash != file.sha512:
        raise CatalogChecksumMismatchError(
            f"expected {file.sha512}, got {computed_hash}"
        )
    sha256 = await ingest_into_cache(cache, content)
    return content, sha256


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
    cache: PluginCacheStore
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
        # Phase 1: Read server metadata (no lock needed).
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)

        content_dir = content_dir_for_server_type(server.server_type)
        loader_type = loader_type_for_server_type(server.server_type)
        loader = modrinth_loader_for_server_type(server.server_type)

        # Phase 2: Fetch from catalog (no lock, no DB).
        versions = await self.catalog.list_versions(
            project_id,
            loader=loader,
            game_versions=[server.mc_version],
        )
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

        if not file.filename.lower().endswith(".jar"):
            raise InvalidFilePathError(file.filename)

        project = await self.catalog.get_project(project_id)

        # Phase 3: Resolve bytes from the cache or download + verify (no lock).
        # The download cache skips the HTTP fetch when this version is cached.
        content, sha256 = await _resolve_modrinth_content(
            uow=self.uow,
            catalog=self.catalog,
            cache=self.cache,
            file=file,
        )

        # Parse the jar manifest for dependency metadata (issue #1307): the
        # uniform source, same as a local upload. Tolerant of an unreadable jar.
        manifest = parse_manifest_at_ingest(content, loader=loader)

        # Side (issue #1308): Modrinth's per-environment support is the most
        # accurate source, so it wins over the jar manifest hint here.
        # Paper plugins are always server-side only (issue #1342).
        if server.server_type is ServerType.PAPER:
            side: PluginSide = "server"
        else:
            side = side_from_modrinth(project.client_side, project.server_side)

        # Capture the version's required catalog deps (issue #1321), keyed by
        # project_id with a display label -- the source manifest deps often miss.
        catalog_dependencies = await capture_catalog_dependencies(self.catalog, version)

        # Phase 4: At-rest gate + write (hold lock, short duration).
        rel_path = f"{content_dir}/{file.filename}"
        async with self.lifecycle_lock.hold(server_id):
            async with self.uow:
                server = await _load(self.uow, community_id, server_id)
                if not server.is_at_rest():
                    raise ServerFilesUnsettledError(str(server_id.value))

                self.file_store.validate_rel_path(rel_path)

                existing = await self.uow.plugins.get_by_rel_path(server_id, rel_path)
                if existing is not None:
                    raise PluginAlreadyExistsError(rel_path)

                # Guard: reject a second version of the same Modrinth project
                # on this server (issue #1332). Two versions of one mod crash
                # or produce undefined behavior at MC runtime.
                by_project = await self.uow.plugins.get_by_source_project_id(
                    server_id, project_id
                )
                if by_project is not None:
                    raise PluginAlreadyExistsError(project_id)

                # Side-aware deploy: a client-only mod is cached + recorded but
                # never placed in the running working set (issue #1308).
                if working_set_present(enabled=True, side=side):
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
                    checksum_sha512=file.sha512,
                    sha256=sha256,
                    size_bytes=len(content),
                    enabled=True,
                    installed_by=installed_by,
                    created_at=now,
                    updated_at=now,
                    mod_identifier=manifest.mod_identifier or None,
                    provides=manifest.provides,
                    dependencies=manifest.dependencies,
                    mc_versions=manifest.mc_versions,
                    side=side,
                    catalog_dependencies=catalog_dependencies,
                )
                await self.uow.plugins.add(plugin)
                await self.uow.commit()
                return plugin


# -- Update check & dependency value objects --


@dataclass(frozen=True)
class PluginUpdateInfo:
    """Update availability for one installed plugin."""

    plugin: ServerPlugin
    latest_version: CatalogVersion | None  # None = no newer version


@dataclass(frozen=True)
class PluginDependencyInfo:
    """One dependency of an installed plugin version."""

    project_id: str
    version_id: str | None
    dependency_type: str
    project_title: str | None
    project_slug: str | None
    installed: bool


# -- Update check use cases --

_CATALOG_CONCURRENCY = 5  # Max parallel Modrinth API calls


async def _check_one(
    catalog: CatalogProvider,
    plugin: ServerPlugin,
    loader: str,
    mc_version: str,
    sem: asyncio.Semaphore,
) -> PluginUpdateInfo:
    """Check a single plugin for updates, bounded by *sem*."""
    async with sem:
        try:
            versions = await catalog.list_versions(
                plugin.source_project_id or "",
                loader=loader,
                game_versions=[mc_version],
            )
        except CatalogUnavailableError:
            return PluginUpdateInfo(plugin=plugin, latest_version=None)
        latest = versions[0] if versions else None
        if latest and latest.version_id != plugin.source_version_id:
            return PluginUpdateInfo(plugin=plugin, latest_version=latest)
        return PluginUpdateInfo(plugin=plugin, latest_version=None)


@dataclass(frozen=True)
class CheckUpdates:
    """Batch check for newer Modrinth versions of all Modrinth-sourced plugins."""

    uow: UnitOfWork
    catalog: CatalogProvider

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
    ) -> list[PluginUpdateInfo]:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
            loader = modrinth_loader_for_server_type(server.server_type)
            plugins = await self.uow.plugins.list_modrinth_plugins(server_id)

        sem = asyncio.Semaphore(_CATALOG_CONCURRENCY)
        results = await asyncio.gather(
            *(
                _check_one(self.catalog, plugin, loader, server.mc_version, sem)
                for plugin in plugins
            )
        )
        return list(results)


@dataclass(frozen=True)
class CheckPluginUpdate:
    """Single-plugin update check."""

    uow: UnitOfWork
    catalog: CatalogProvider

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        plugin_id: PluginId,
    ) -> PluginUpdateInfo:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
            plugin = await self.uow.plugins.get_by_id(server_id, plugin_id)
        if plugin is None:
            raise PluginNotFoundError(str(plugin_id.value))
        if plugin.source is not PluginSource.MODRINTH:
            return PluginUpdateInfo(plugin=plugin, latest_version=None)

        loader = modrinth_loader_for_server_type(server.server_type)
        versions = await self.catalog.list_versions(
            plugin.source_project_id or "",
            loader=loader,
            game_versions=[server.mc_version],
        )
        latest = versions[0] if versions else None
        if latest and latest.version_id != plugin.source_version_id:
            return PluginUpdateInfo(plugin=plugin, latest_version=latest)
        return PluginUpdateInfo(plugin=plugin, latest_version=None)


@dataclass(frozen=True)
class UpdatePlugin:
    """Download a newer catalog version and replace the installed jar.

    Follows the same phased pattern as :class:`InstallFromCatalog`.
    """

    uow: UnitOfWork
    catalog: CatalogProvider
    file_store: FileStore
    cache: PluginCacheStore
    clock: Clock
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        plugin_id: PluginId,
        version_id: str,
    ) -> ServerPlugin:
        # Phase 1: Read server + plugin metadata (no lock).
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
            plugin = await self.uow.plugins.get_by_id(server_id, plugin_id)
        if plugin is None:
            raise PluginNotFoundError(str(plugin_id.value))
        if plugin.source is not PluginSource.MODRINTH:
            raise PluginNotFoundError(str(plugin_id.value))

        content_dir = content_dir_for_server_type(server.server_type)
        loader = modrinth_loader_for_server_type(server.server_type)

        # Phase 2: Fetch version from catalog, select primary file, validate .jar.
        versions = await self.catalog.list_versions(
            plugin.source_project_id or "",
            loader=loader,
            game_versions=[server.mc_version],
        )
        version = next((v for v in versions if v.version_id == version_id), None)
        if version is None:
            raise CatalogProjectNotFoundError(
                f"version {version_id} not found for project {plugin.source_project_id}"
            )

        primary = next((f for f in version.files if f.primary), None)
        if primary is None and version.files:
            primary = version.files[0]
        if primary is None:
            raise CatalogProjectNotFoundError(f"no files in version {version_id}")
        file = primary

        if not file.filename.lower().endswith(".jar"):
            raise InvalidFilePathError(file.filename)

        # Phase 3: Resolve bytes from the cache or download + verify (no lock).
        # The download cache skips the HTTP fetch when this version is cached.
        content, sha256 = await _resolve_modrinth_content(
            uow=self.uow,
            catalog=self.catalog,
            cache=self.cache,
            file=file,
        )

        # Re-parse the new jar's manifest so the dependency metadata tracks the
        # updated version (issue #1307). Tolerant of an unreadable jar.
        manifest = parse_manifest_at_ingest(content, loader=loader)

        # Re-capture the new version's required catalog deps (issue #1321) so they
        # track the update, same as the manifest metadata above.
        catalog_dependencies = await capture_catalog_dependencies(self.catalog, version)

        # Phase 4: At-rest gate + write (hold lock, short duration). The new jar's
        # working-set path follows the (enabled, side) desired state (issue #1308):
        # a disabled server/both jar lives at the .disabled path, a client jar has
        # no file. Reconciling old -> new keeps rel_path and the on-disk file
        # consistent and never orphans the prior .disabled file.
        new_clean_path = f"{content_dir}/{file.filename}"
        old_path = working_set_path(
            clean_path=plugin.rel_path.removesuffix(".disabled"),
            enabled=plugin.enabled,
            side=plugin.side,
        )
        new_path = working_set_path(
            clean_path=new_clean_path, enabled=plugin.enabled, side=plugin.side
        )
        async with self.lifecycle_lock.hold(server_id):
            async with self.uow:
                server = await _load(self.uow, community_id, server_id)
                if not server.is_at_rest():
                    raise ServerFilesUnsettledError(str(server_id.value))

                self.file_store.validate_rel_path(new_clean_path)

                if new_path is not None and new_path != old_path:
                    existing = await self.uow.plugins.get_by_rel_path(
                        server_id, new_path
                    )
                    if existing is not None and existing.id != plugin_id:
                        raise PluginAlreadyExistsError(new_path)

                # Write the fresh bytes at the desired path and remove the old file
                # (also when only the side/enabled mapping leaves the new bytes at a
                # different path). A client-only jar has no working-set file.
                if new_path is not None:
                    await self.file_store.write_file(
                        community_id=community_id,
                        server_id=server_id,
                        rel_path=new_path,
                        content=content,
                    )
                if old_path is not None and old_path != new_path:
                    try:
                        await self.file_store.delete_file(
                            community_id=community_id,
                            server_id=server_id,
                            rel_path=old_path,
                        )
                    except ServerFileNotFoundError:
                        pass

                now = self.clock.now()
                updated_plugin = ServerPlugin(
                    id=plugin.id,
                    server_id=plugin.server_id,
                    rel_path=new_path if new_path is not None else new_clean_path,
                    filename=file.filename,
                    display_name=plugin.display_name,
                    description=plugin.description,
                    loader_type=plugin.loader_type,
                    source=plugin.source,
                    source_project_id=plugin.source_project_id,
                    source_version_id=version_id,
                    version_number=version.version_number,
                    checksum_sha512=file.sha512,
                    sha256=sha256,
                    size_bytes=len(content),
                    enabled=plugin.enabled,
                    installed_by=plugin.installed_by,
                    created_at=plugin.created_at,
                    updated_at=now,
                    mod_identifier=manifest.mod_identifier or None,
                    provides=manifest.provides,
                    dependencies=manifest.dependencies,
                    mc_versions=manifest.mc_versions,
                    side=plugin.side,
                    catalog_dependencies=catalog_dependencies,
                )
                await self.uow.plugins.update(updated_plugin)
                await self.uow.commit()
                return updated_plugin


@dataclass(frozen=True)
class ListPluginDependencies:
    """List dependencies for an installed plugin version."""

    uow: UnitOfWork
    catalog: CatalogProvider

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        plugin_id: PluginId,
    ) -> list[PluginDependencyInfo]:
        async with self.uow:
            await _load(self.uow, community_id, server_id)
            plugin = await self.uow.plugins.get_by_id(server_id, plugin_id)
            all_plugins = await self.uow.plugins.list_modrinth_plugins(server_id)
        if plugin is None:
            raise PluginNotFoundError(str(plugin_id.value))
        if plugin.source is not PluginSource.MODRINTH:
            return []

        versions = await self.catalog.list_versions(plugin.source_project_id or "")
        installed_version = next(
            (v for v in versions if v.version_id == plugin.source_version_id),
            None,
        )
        if installed_version is None:
            return []

        installed_project_ids = {
            p.source_project_id for p in all_plugins if p.source_project_id
        }
        sem = asyncio.Semaphore(_CATALOG_CONCURRENCY)

        async def _fetch_dep(dep: CatalogDependency) -> PluginDependencyInfo:
            async with sem:
                project_title: str | None = None
                project_slug: str | None = None
                try:
                    proj = await self.catalog.get_project(dep.project_id)
                    project_title = proj.title
                    project_slug = proj.slug
                except (CatalogUnavailableError, CatalogProjectNotFoundError):
                    pass
                return PluginDependencyInfo(
                    project_id=dep.project_id,
                    version_id=dep.version_id,
                    dependency_type=dep.dependency_type,
                    project_title=project_title,
                    project_slug=project_slug,
                    installed=dep.project_id in installed_project_ids,
                )

        results = await asyncio.gather(
            *(_fetch_dep(dep) for dep in installed_version.dependencies)
        )
        return list(results)
