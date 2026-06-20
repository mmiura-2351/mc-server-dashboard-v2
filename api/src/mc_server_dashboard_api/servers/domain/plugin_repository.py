"""Persistence Port for plugin metadata.

The ``PluginRepository`` the plugin use cases depend on; a concrete
async-SQLAlchemy adapter implements it on the unit-of-work's session. Lookups
return ``None`` when absent rather than raising, so callers decide policy
(mirroring :class:`BackupRepository`).
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.servers.domain.plugin import PluginId, ServerPlugin
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


class PluginRepository(abc.ABC):
    """Port: persistence for :class:`ServerPlugin` metadata rows."""

    @abc.abstractmethod
    async def add(self, plugin: ServerPlugin) -> None:
        """Stage a new plugin row for persistence within the current transaction."""

    @abc.abstractmethod
    async def get_by_id(
        self, server_id: ServerId, plugin_id: PluginId
    ) -> ServerPlugin | None:
        """Return the plugin with ``plugin_id`` scoped to ``server_id``, or ``None``."""

    @abc.abstractmethod
    async def list_for_server(self, server_id: ServerId) -> list[ServerPlugin]:
        """Return a server's plugins ordered by display_name."""

    @abc.abstractmethod
    async def delete(self, plugin_id: PluginId) -> None:
        """Delete the plugin row."""

    @abc.abstractmethod
    async def get_by_rel_path(
        self, server_id: ServerId, rel_path: str
    ) -> ServerPlugin | None:
        """Return the plugin at ``rel_path`` scoped to ``server_id``, or ``None``."""

    @abc.abstractmethod
    async def update(self, plugin: ServerPlugin) -> None:
        """Full entity update of the plugin row."""

    @abc.abstractmethod
    async def list_modrinth_plugins(self, server_id: ServerId) -> list[ServerPlugin]:
        """Return plugins with source=MODRINTH and a non-null source_project_id."""

    @abc.abstractmethod
    async def find_sha256_by_sha512(self, checksum_sha512: str) -> str | None:
        """Return a cached SHA-256 content address for a known SHA-512, or ``None``.

        The download-cache lookup (issue #1306): a Modrinth version's published
        SHA-512 maps to the SHA-256 of an already-cached jar, so the same version
        is served from the cache instead of being re-downloaded per server.
        """
