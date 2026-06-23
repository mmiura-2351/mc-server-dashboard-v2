"""Client modpack use cases (issue #1308).

Players only need the *client* mods of a server's set. These read-only use cases
expose them per server: :class:`ListClientMods` lists the enabled plugins whose
side is client-relevant ({``client``, ``both``}), and
:class:`DownloadClientModpack` streams those jars as a single zip from the
content-addressed cache (the same blobs the install path stored). Both are
``plugin:read``; neither mutates the working set.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from mc_server_dashboard_api.servers.application.client_modpack_zip import (
    stream_client_modpack,
)
from mc_server_dashboard_api.servers.domain.errors import ServerNotFoundError
from mc_server_dashboard_api.servers.domain.plugin import (
    ServerPlugin,
    content_dir_for_server_type,
)
from mc_server_dashboard_api.servers.domain.plugin_cache_store import PluginCacheStore
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId


def _is_client_mod(plugin: ServerPlugin) -> bool:
    """Whether ``plugin`` belongs in the client modpack (enabled, client-relevant)."""

    return plugin.enabled and plugin.side in ("client", "both")


async def _client_mods(
    uow: UnitOfWork, community_id: CommunityId, server_id: ServerId
) -> list[ServerPlugin]:
    server = await uow.servers.get_by_id(server_id)
    if server is None or server.community_id != community_id:
        raise ServerNotFoundError(str(server_id.value))
    # Verify this server type supports plugins (raises if not).
    content_dir_for_server_type(server.server_type)
    plugins = await uow.plugins.list_for_server(server_id)
    return [p for p in plugins if _is_client_mod(p)]


@dataclass(frozen=True)
class ListClientMods:
    """List a server's enabled client-relevant plugins (plugin:read, issue #1308)."""

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> list[ServerPlugin]:
        async with self.uow:
            return await _client_mods(self.uow, community_id, server_id)


@dataclass(frozen=True)
class DownloadClientModpack:
    """Stream a server's client mods as a zip (plugin:read, issue #1308).

    Resolves the enabled client-relevant plugins, then streams their jars from
    the content-addressed cache into a single zip with bounded memory.
    """

    uow: UnitOfWork
    cache: PluginCacheStore

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> AsyncIterator[bytes]:
        async with self.uow:
            mods = await _client_mods(self.uow, community_id, server_id)
        return stream_client_modpack(self.cache, mods)
