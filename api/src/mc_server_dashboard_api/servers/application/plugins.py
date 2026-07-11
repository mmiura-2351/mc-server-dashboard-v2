"""Plugin/mod content management use cases (issue #1150).

All mutations require the server at rest and hold the per-server lifecycle lock.
The at-rest gate and lock pattern mirrors the file and backup use cases.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.servers.application.plugin_cache import (
    ingest_into_cache,
)
from mc_server_dashboard_api.servers.application.plugin_manifest import (
    parse_manifest_at_ingest,
)
from mc_server_dashboard_api.servers.application.plugin_validation import (
    PluginValidation,
    validate_plugin_set,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    InvalidFilePathError,
    InvalidPluginSideError,
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
    is_geyser_plugin,
    loader_type_for_server_type,
    modrinth_loader_for_server_type,
    sanitize_plugin_filename,
    working_set_path,
    working_set_present,
)
from mc_server_dashboard_api.servers.domain.plugin_cache_store import PluginCacheStore
from mc_server_dashboard_api.servers.domain.ports import (
    PortRange,
    pick_lowest_free_port,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
    ServerType,
)

# Plugin upload size cap: same as the file upload cap (512 MiB).
MAX_PLUGIN_BYTES = 512 * 1024 * 1024

# The accepted values for a manual side override (issue #1308).
_VALID_SIDES: frozenset[str] = frozenset({"server", "client", "both"})


async def _load(
    uow: UnitOfWork, community_id: CommunityId, server_id: ServerId
) -> Server:
    server = await uow.servers.get_by_id(server_id)
    if server is None or server.community_id != community_id:
        raise ServerNotFoundError(str(server_id.value))
    return server


async def _reconcile_working_set(
    *,
    file_store: FileStore,
    cache: PluginCacheStore,
    community_id: CommunityId,
    server_id: ServerId,
    sha256: str | None,
    current_path: str | None,
    desired_path: str | None,
) -> None:
    """Drive the working-set file from its current location to the desired one.

    A single reconcile step (issue #1308) replaces the per-transition file ops so
    ``TogglePlugin`` / ``SetPluginSide`` / ``UpdatePlugin`` stay consistent:

    * desired absent, currently present -> remove.
    * desired present, currently absent -> materialize the cached bytes.
    * desired present elsewhere         -> rename (no content buffering).
    * already at the desired path / both absent -> noop.

    ``current_path`` / ``desired_path`` are derived from ``(enabled, side)`` via
    :func:`working_set_path`, so the on-disk file always matches the plugin's
    recorded state and the rename branch never targets its own path.
    """

    if current_path == desired_path:
        return

    if desired_path is None:
        # Remove the working-set file (the cached blob stays).
        try:
            await file_store.delete_file(
                community_id=community_id,
                server_id=server_id,
                rel_path=current_path,  # type: ignore[arg-type]
            )
        except ServerFileNotFoundError:
            pass
        return

    if current_path is None:
        # Materialize from the content-addressed cache.
        if sha256 is None:
            return
        content = b"".join([chunk async for chunk in cache.open(sha256)])
        await file_store.write_file(
            community_id=community_id,
            server_id=server_id,
            rel_path=desired_path,
            content=content,
        )
        return

    # Rename within the working set (issue #1164): use rename_file to avoid
    # reading the full JAR into memory and to skip version retention (a rename
    # does not change the content). If the on-disk file is missing (e.g.
    # external deletion via Files API or a backup restore), fall back to
    # materializing from the content-addressed cache instead of 500-ing
    # (issue #1331 defence-in-depth).
    try:
        await file_store.rename_file(
            community_id=community_id,
            server_id=server_id,
            from_path=current_path,
            to_path=desired_path,
        )
    except ServerFileNotFoundError:
        if sha256 is None:
            return
        content = b"".join([chunk async for chunk in cache.open(sha256)])
        await file_store.write_file(
            community_id=community_id,
            server_id=server_id,
            rel_path=desired_path,
            content=content,
        )


async def allocate_bedrock_port_if_geyser(
    uow: UnitOfWork,
    *,
    server: Server,
    plugin: ServerPlugin,
    port_range: PortRange | None,
    now: dt.datetime,
) -> None:
    """Allocate the server's public Bedrock UDP port when Geyser arrives (#1541).

    Installing Geyser as a normal plugin is the Bedrock enablement switch: when
    the freshly ingested ``plugin`` is Geyser, the deployment gate is on
    (``port_range`` is non-None -- relay enabled + Bedrock capability), and the
    server holds no port yet, the lowest free port in the dedicated UDP window is
    staged onto the server row. Runs inside the install transaction so the plugin
    row and the port commit atomically; ``UNIQUE(bedrock_port)`` backstops a
    concurrent racer. Raises :class:`PortRangeExhaustedError` (aborting the
    install) when the window has no free port.
    """

    if port_range is None or server.bedrock_port is not None:
        return
    if not is_geyser_plugin(plugin):
        return
    taken = await uow.servers.list_bedrock_ports()
    server.bedrock_port = pick_lowest_free_port(port_range, taken=taken)
    server.updated_at = now
    await uow.servers.update(server)


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
class ValidatePluginSet:
    """Validate a server's installed plugin set (plugin:read, issue #1307).

    Loads the server (for its loader + MC version) and its installed plugins,
    then runs the pure :func:`validate_plugin_set` phase-B checklist. Display
    only: it never mutates the set. Runnable on demand from the WebUI and at
    assignment time after an install/update.
    """

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> PluginValidation:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
            # Verify this server type supports plugins (raises if not).
            content_dir_for_server_type(server.server_type)
            plugins = await self.uow.plugins.list_for_server(server_id)
        return validate_plugin_set(
            server_type=server.server_type.value,
            mc_version=server.mc_version,
            plugins=plugins,
        )


@dataclass(frozen=True)
class GetPlugin:
    """Fetch a single installed plugin by id (plugin:read)."""

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId, plugin_id: PluginId
    ) -> ServerPlugin:
        async with self.uow:
            await _load(self.uow, community_id, server_id)
            plugin = await self.uow.plugins.get_by_id(server_id, plugin_id)
        if plugin is None:
            raise PluginNotFoundError(str(plugin_id.value))
        return plugin


@dataclass(frozen=True)
class InstallPlugin:
    """Install a local plugin jar into the server's content directory.

    ``bedrock_port_range`` is the deployment's assignable Bedrock UDP window
    (issue #1541), or ``None`` when the Bedrock deployment gate is off; a Geyser
    jar detected at ingest allocates the server's ``bedrock_port`` from it.
    """

    uow: UnitOfWork
    file_store: FileStore
    cache: PluginCacheStore
    clock: Clock
    lifecycle_lock: LifecycleLock = NullLifecycleLock()
    bedrock_port_range: PortRange | None = None

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
        if not filename.lower().endswith(".jar"):
            raise InvalidFilePathError(filename)
        filename = sanitize_plugin_filename(filename)

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

                # Parse the jar manifest for dependency metadata (issue #1307);
                # tolerant of an unreadable jar (the install still proceeds).
                manifest = parse_manifest_at_ingest(
                    content,
                    loader=modrinth_loader_for_server_type(server.server_type),
                )

                # Ingest into the content-addressed cache (dedup-on-ingest). The
                # jar is the byte source for the working set; a client-only mod is
                # cached but never deployed (issue #1306, #1308).
                sha256 = await ingest_into_cache(self.cache, content)

                # Paper plugins are always server-side only (issue #1342).
                if server.server_type is ServerType.PAPER:
                    side: PluginSide = "server"
                else:
                    side = manifest.side

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
                )
                await self.uow.plugins.add(plugin)
                # Geyser detection (issue #1541): a Geyser jar switches the
                # server Bedrock-on, allocating its public UDP port in the same
                # transaction as the plugin row.
                await allocate_bedrock_port_if_geyser(
                    self.uow,
                    server=server,
                    plugin=plugin,
                    port_range=self.bedrock_port_range,
                    now=now,
                )
                await self.uow.commit()
                # Side-aware deploy (issue #1308): only a server-relevant,
                # enabled jar goes into the working set; a client-only jar is
                # tracked + cached but never written there. The write follows
                # the commit so a commit failure cannot orphan a working-set
                # jar with no DB row (issue #1706).
                if working_set_present(enabled=True, side=side):
                    await self.file_store.write_file(
                        community_id=community_id,
                        server_id=server_id,
                        rel_path=rel_path,
                        content=content,
                    )
                return plugin


@dataclass(frozen=True)
class RemovePlugin:
    """Remove an installed plugin (delete jar + DB record).

    Removing Geyser switches the server Bedrock-off (issue #1541): the server's
    ``bedrock_port`` is released (set back to NULL) in the same transaction,
    unless another installed Geyser jar remains.
    """

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
    ) -> None:
        async with self.lifecycle_lock.hold(server_id):
            async with self.uow:
                server = await _load(self.uow, community_id, server_id)
                if not server.is_at_rest():
                    raise ServerFilesUnsettledError(str(server_id.value))

                plugin = await self.uow.plugins.get_by_id(server_id, plugin_id)
                if plugin is None:
                    raise PluginNotFoundError(str(plugin_id.value))

                rel_path = plugin.rel_path
                await self.uow.plugins.delete(plugin_id)
                # Release the Bedrock port when the last Geyser leaves (issue
                # #1541). Unconditional on the deployment gate: a port allocated
                # while the gate was on must not leak if the gate is off now.
                if server.bedrock_port is not None and is_geyser_plugin(plugin):
                    remaining = await self.uow.plugins.list_for_server(server_id)
                    if not any(is_geyser_plugin(p) for p in remaining):
                        server.bedrock_port = None
                        server.updated_at = self.clock.now()
                        await self.uow.servers.update(server)
                await self.uow.commit()
                # Delete the working-set file after the DB commit so a commit
                # failure does not leave a live row whose jar is gone (#1706).
                try:
                    await self.file_store.delete_file(
                        community_id=community_id,
                        server_id=server_id,
                        rel_path=rel_path,
                    )
                except ServerFileNotFoundError:
                    pass  # jar already gone


@dataclass(frozen=True)
class TogglePlugin:
    """Enable or disable a plugin, reconciling the working set to the new state.

    The desired on-disk state is a function of ``(enabled, side)`` (issue #1308):
    a server/both jar lives at the clean path when enabled and the ``.disabled``
    path when disabled; a client-only jar has no working-set file at all. The
    reconcile step computes the right action (rename / materialize-from-cache /
    remove / noop) from the current and desired paths, so enabling a jar that
    became server/both while disabled materializes from the cache rather than
    self-colliding on a no-op rename.
    """

    uow: UnitOfWork
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

                clean_path = plugin.rel_path.removesuffix(".disabled")
                current_path = working_set_path(
                    clean_path=clean_path, enabled=plugin.enabled, side=plugin.side
                )
                new_path = working_set_path(
                    clean_path=clean_path, enabled=enable, side=plugin.side
                )

                # Block a cross-plugin collision on the target path (a different
                # row already occupying it); the plugin's own row never counts.
                if new_path is not None and new_path != current_path:
                    existing = await self.uow.plugins.get_by_rel_path(
                        server_id, new_path
                    )
                    if existing is not None and existing.id != plugin.id:
                        raise PluginAlreadyExistsError(new_path)

                plugin.enabled = enable
                plugin.rel_path = new_path if new_path is not None else clean_path
                plugin.updated_at = self.clock.now()
                await self.uow.plugins.update(plugin)
                await self.uow.commit()
                # Reconcile the working-set file after the DB commit so a
                # commit failure does not leave rel_path diverged (#1706).
                await _reconcile_working_set(
                    file_store=self.file_store,
                    cache=self.cache,
                    community_id=community_id,
                    server_id=server_id,
                    sha256=plugin.sha256,
                    current_path=current_path,
                    desired_path=new_path,
                )
                return plugin


@dataclass(frozen=True)
class SetPluginSide:
    """Override an installed plugin's side, re-materializing the working set.

    The side (``server`` / ``client`` / ``both``) governs working-set presence
    (issue #1308): the running server holds exactly the enabled jars with side in
    {``server``, ``both``}. Changing the side may flip that presence:

    * client -> {server, both} (and enabled): materialize the jar into the
      working set from the content-addressed cache.
    * {server, both} -> client: remove the working-set file.

    The cache is the byte source, so no re-upload is needed. At-rest gated like
    every other plugin mutation (409 ``server_unsettled`` while running).
    """

    uow: UnitOfWork
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
        side: str,
    ) -> ServerPlugin:
        if side not in _VALID_SIDES:
            raise InvalidPluginSideError(side)
        new_side: PluginSide = side  # type: ignore[assignment]

        async with self.lifecycle_lock.hold(server_id):
            async with self.uow:
                server = await _load(self.uow, community_id, server_id)
                # Paper plugins are always server-side only (issue #1342).
                if server.server_type is ServerType.PAPER and new_side != "server":
                    raise InvalidPluginSideError(
                        "paper servers only support server side"
                    )
                if not server.is_at_rest():
                    raise ServerFilesUnsettledError(str(server_id.value))

                plugin = await self.uow.plugins.get_by_id(server_id, plugin_id)
                if plugin is None:
                    raise PluginNotFoundError(str(plugin_id.value))

                if plugin.side == new_side:
                    return plugin

                clean_path = plugin.rel_path.removesuffix(".disabled")
                current_path = working_set_path(
                    clean_path=clean_path, enabled=plugin.enabled, side=plugin.side
                )
                desired_path = working_set_path(
                    clean_path=clean_path, enabled=plugin.enabled, side=new_side
                )

                # Block a cross-plugin collision on the target path (a different
                # row already occupying it); the plugin's own row never counts.
                if desired_path is not None and desired_path != current_path:
                    existing = await self.uow.plugins.get_by_rel_path(
                        server_id, desired_path
                    )
                    if existing is not None and existing.id != plugin.id:
                        raise PluginAlreadyExistsError(desired_path)

                plugin.side = new_side
                plugin.rel_path = (
                    desired_path if desired_path is not None else clean_path
                )
                plugin.updated_at = self.clock.now()
                await self.uow.plugins.update(plugin)
                await self.uow.commit()
                # Reconcile the working-set file after the DB commit so a
                # commit failure does not leave the file diverged (#1706).
                await _reconcile_working_set(
                    file_store=self.file_store,
                    cache=self.cache,
                    community_id=community_id,
                    server_id=server_id,
                    sha256=plugin.sha256,
                    current_path=current_path,
                    desired_path=desired_path,
                )
                return plugin
