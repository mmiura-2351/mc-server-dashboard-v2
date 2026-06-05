"""Read use cases for the version catalog endpoints (FR-VER-1).

Thin wrappers over the :class:`VersionCatalog` Port. The types index is the set of
server types the catalog can resolve (``vanilla`` / ``paper`` / ``fabric`` /
``forge``) — it is the catalog's own ``ServerType`` enum, so ``spigot`` is absent
by construction.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.versions.domain.catalog import VersionCatalog
from mc_server_dashboard_api.versions.domain.value_objects import (
    ServerType,
    VersionRef,
)


@dataclass(frozen=True)
class ListVersions:
    """List the versions offered for a server type (server-type read)."""

    catalog: VersionCatalog

    async def __call__(self, *, server_type: ServerType) -> list[VersionRef]:
        return await self.catalog.list_versions(server_type)


@dataclass(frozen=True)
class ListServerTypes:
    """List the server types the catalog can resolve at M1."""

    async def __call__(self) -> list[ServerType]:
        return list(ServerType)
