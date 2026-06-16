"""Plugin/mod content management use cases (issue #1150).

All mutations require the server at rest and hold the per-server lifecycle lock.
The at-rest gate and lock pattern mirrors the file and backup use cases.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
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
    PluginSource,
    ServerPlugin,
    content_dir_for_server_type,
    loader_type_for_server_type,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId

# Plugin upload size cap: same as the file upload cap (512 MiB).
MAX_PLUGIN_BYTES = 512 * 1024 * 1024


async def _load(
    uow: UnitOfWork, community_id: CommunityId, server_id: ServerId
) -> Server:
    server = await uow.servers.get_by_id(server_id)
    if server is None or server.community_id != community_id:
        raise ServerNotFoundError(str(server_id.value))
    return server


@dataclass(frozen=True)
class ListPlugins:
    """List installed plugins for a server (plugin:read)."""

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> list[ServerPlugin]:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
            # Verify this server type supports plugins (raises if not).
            content_dir_for_server_type(server.server_type)
            return await self.uow.plugins.list_for_server(server_id)


@dataclass(frozen=True)
class InstallPlugin:
    """Install a local plugin jar into the server's content directory."""

    uow: UnitOfWork
    file_store: FileStore
    clock: Clock
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        filename: str,
        display_name: str,
        content: bytes,
        installed_by: uuid.UUID | None = None,
    ) -> ServerPlugin:
        if len(content) > MAX_PLUGIN_BYTES:
            raise FileTooLargeError(str(len(content)))
        if not filename.endswith(".jar"):
            raise InvalidFilePathError(filename)

        async with self.lifecycle_lock.hold(server_id):
            async with self.uow:
                server = await _load(self.uow, community_id, server_id)
                if not server.is_at_rest():
                    raise ServerFilesUnsettledError(str(server_id.value))

                content_dir = content_dir_for_server_type(server.server_type)
                loader_type = loader_type_for_server_type(server.server_type)
                rel_path = f"{content_dir}/{filename}"
                self.file_store.validate_rel_path(rel_path)

                existing = await self.uow.plugins.get_by_rel_path(server_id, rel_path)
                if existing is not None:
                    raise PluginAlreadyExistsError(rel_path)

                # Write jar to storage.
                await self.file_store.write_file(
                    community_id=community_id,
                    server_id=server_id,
                    rel_path=rel_path,
                    content=content,
                )

                checksum = hashlib.sha512(content).hexdigest()
                now = self.clock.now()

                plugin = ServerPlugin(
                    id=PluginId.new(),
                    server_id=server_id,
                    rel_path=rel_path,
                    filename=filename,
                    display_name=display_name,
                    description=None,
                    loader_type=loader_type,
                    source=PluginSource.LOCAL,
                    source_project_id=None,
                    source_version_id=None,
                    version_number=None,
                    checksum_sha512=checksum,
                    size_bytes=len(content),
                    enabled=True,
                    installed_by=installed_by,
                    created_at=now,
                    updated_at=now,
                )
                await self.uow.plugins.add(plugin)
                await self.uow.commit()
                return plugin


@dataclass(frozen=True)
class RemovePlugin:
    """Remove an installed plugin (delete jar + DB record)."""

    uow: UnitOfWork
    file_store: FileStore
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        plugin_id: PluginId,
    ) -> None:
        async with self.lifecycle_lock.hold(server_id):
            async with self.uow:
                server = await _load(self.uow, community_id, server_id)
                if not server.is_at_rest():
                    raise ServerFilesUnsettledError(str(server_id.value))

                plugin = await self.uow.plugins.get_by_id(server_id, plugin_id)
                if plugin is None:
                    raise PluginNotFoundError(str(plugin_id.value))

                try:
                    await self.file_store.delete_file(
                        community_id=community_id,
                        server_id=server_id,
                        rel_path=plugin.rel_path,
                    )
                except ServerFileNotFoundError:
                    pass  # jar already gone; proceed with DB cleanup
                await self.uow.plugins.delete(plugin_id)
                await self.uow.commit()


@dataclass(frozen=True)
class TogglePlugin:
    """Enable or disable a plugin via the .disabled suffix rename convention."""

    uow: UnitOfWork
    file_store: FileStore
    clock: Clock
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        plugin_id: PluginId,
        enable: bool,
    ) -> ServerPlugin:
        async with self.lifecycle_lock.hold(server_id):
            async with self.uow:
                server = await _load(self.uow, community_id, server_id)
                if not server.is_at_rest():
                    raise ServerFilesUnsettledError(str(server_id.value))

                plugin = await self.uow.plugins.get_by_id(server_id, plugin_id)
                if plugin is None:
                    raise PluginNotFoundError(str(plugin_id.value))

                # Already in desired state: no-op.
                if plugin.enabled == enable:
                    return plugin

                old_path = plugin.rel_path
                if enable:
                    # Strip .disabled suffix.
                    new_path = old_path.removesuffix(".disabled")
                else:
                    # Append .disabled suffix.
                    new_path = f"{old_path}.disabled"

                existing = await self.uow.plugins.get_by_rel_path(server_id, new_path)
                if existing is not None:
                    raise PluginAlreadyExistsError(new_path)

                # Rename: read old -> write new -> delete old (same pattern as
                # RenameFile in files.py).
                content = await self.file_store.read_file(
                    community_id=community_id,
                    server_id=server_id,
                    rel_path=old_path,
                )
                await self.file_store.write_file(
                    community_id=community_id,
                    server_id=server_id,
                    rel_path=new_path,
                    content=content,
                )
                await self.file_store.delete_file(
                    community_id=community_id,
                    server_id=server_id,
                    rel_path=old_path,
                )

                plugin.enabled = enable
                plugin.rel_path = new_path
                plugin.updated_at = self.clock.now()
                await self.uow.plugins.update(plugin)
                await self.uow.commit()
                return plugin
