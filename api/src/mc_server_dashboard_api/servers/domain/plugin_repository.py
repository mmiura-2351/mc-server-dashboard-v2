"""Persistence Port for plugin metadata.

The ``PluginRepository`` the plugin use cases depend on; a concrete
async-SQLAlchemy adapter implements it on the unit-of-work's session. Lookups
return ``None`` when absent rather than raising, so callers decide policy
(mirroring :class:`BackupRepository`).
"""

from __future__ import annotations

import abc
from collections.abc import Iterable
from typing import NamedTuple

from mc_server_dashboard_api.servers.domain.plugin import (
    PluginId,
    PluginSource,
    ServerPlugin,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


class CatalogProvenance(NamedTuple):
    """Structured return for catalog-provenance recovery lookups."""

    source: PluginSource
    project_id: str
    source_version_id: str | None
    version_number: str | None


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
    async def enabled_geyser_server_ids(
        self, server_ids: Iterable[ServerId]
    ) -> set[ServerId]:
        """Return the subset of ``server_ids`` with at least one enabled Geyser copy.

        The batched counterpart to ``list_for_server`` (issue #1555): a caller that
        must classify many servers' Bedrock-joinable state at once (the servers list
        response gate) uses this instead of one ``list_for_server`` call per server,
        so listing a community's servers stays a fixed number of queries regardless
        of how many servers it has. Applies the same enabled-Geyser predicate as the
        single-server callers (``has_enabled_geyser``).
        """

    @abc.abstractmethod
    async def delete(self, plugin_id: PluginId) -> None:
        """Delete the plugin row."""

    @abc.abstractmethod
    async def get_by_rel_path(
        self, server_id: ServerId, rel_path: str
    ) -> ServerPlugin | None:
        """Return the plugin occupying ``rel_path`` scoped to ``server_id``.

        Returns ``None`` when the slot is empty. A trailing ``.disabled`` suffix is
        normalized on both the query and the stored path (issue #1316), so a clean
        path and its disabled variant share one per-server slot: a disabled plugin
        still blocks a same-named install.
        """

    @abc.abstractmethod
    async def update(self, plugin: ServerPlugin) -> None:
        """Full entity update of the plugin row."""

    @abc.abstractmethod
    async def list_catalog_plugins(self, server_id: ServerId) -> list[ServerPlugin]:
        """Return catalog-sourced plugins with a non-null source_project_id.

        Catalog-sourced means MODRINTH or GEYSER (see ``CATALOG_SOURCES``): both
        resolve their latest version through the catalog seam, so both are
        update-checkable (issue #1916). LOCAL uploads are excluded.
        """

    @abc.abstractmethod
    async def get_by_source_project_id(
        self, server_id: ServerId, source_project_id: str
    ) -> ServerPlugin | None:
        """Return the plugin with ``source_project_id`` scoped to ``server_id``.

        Returns ``None`` when no plugin on the server originates from the given
        catalog project. Used to prevent installing two versions of the same
        Modrinth project on one server (issue #1332).
        """

    @abc.abstractmethod
    async def all_sha256s(self) -> set[str]:
        """Return every distinct non-null ``sha256`` across all servers.

        The plugin-cache GC's reference set (issue #1332): a cached blob
        whose sha256 is in this set is still referenced by at least one
        installed plugin and must not be reclaimed.
        """

    @abc.abstractmethod
    async def find_catalog_provenance_by_sha512(
        self, checksum_sha512: str
    ) -> CatalogProvenance | None:
        """Return catalog provenance for a known SHA-512.

        Returns a :class:`CatalogProvenance` when a catalog-sourced plugin with
        a matching checksum exists, or ``None`` when no match is found.

        The provenance-recovery lookup behind ghost re-ingestion (issue #2059):
        a jar re-ingested after a backup restore carries no DB row of its own, so
        its origin is matched against the checksum of any catalog-sourced plugin
        (``source`` in ``CATALOG_SOURCES``, non-null ``source_project_id``)
        installed anywhere, using the ``ix_server_plugin_checksum_sha512`` index.
        ``source_version_id`` and ``version_number`` are recovered alongside
        (issue #2068) so that the update check does not report a spurious update.
        Returns ``None`` when no catalog install shares the checksum, so the
        caller marks the row provenance-unknown instead of asserting ``local``.
        """

    @abc.abstractmethod
    async def find_sha256_by_sha512(self, checksum_sha512: str) -> str | None:
        """Return a cached SHA-256 content address for a known SHA-512, or ``None``.

        The download-cache lookup (issue #1306): a Modrinth version's published
        SHA-512 maps to the SHA-256 of an already-cached jar, so the same version
        is served from the cache instead of being re-downloaded per server.
        """
