"""Composite catalog: dispatch a request to the per-type catalog (FR-VER-1).

The endpoint and the ensure-on-start use case depend on one
:class:`VersionCatalog`; this adapter routes ``(server_type, ...)`` to the
matching per-type catalog (vanilla -> Mojang, paper -> PaperMC). A server type
with no registered catalog is :class:`UnknownServerTypeError` — this is the seam
where ``forge`` is rejected as unsupported at M1 (it has no catalog).
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.versions.domain.catalog import VersionCatalog
from mc_server_dashboard_api.versions.domain.errors import UnknownServerTypeError
from mc_server_dashboard_api.versions.domain.value_objects import (
    JarSource,
    ServerType,
    VersionRef,
)


@dataclass(frozen=True)
class CompositeCatalog(VersionCatalog):
    """Route catalog requests to the per-server-type catalog."""

    by_type: dict[ServerType, VersionCatalog]

    async def list_versions(self, server_type: ServerType) -> list[VersionRef]:
        return await self._for(server_type).list_versions(server_type)

    async def resolve(self, server_type: ServerType, version: str) -> JarSource:
        return await self._for(server_type).resolve(server_type, version)

    def _for(self, server_type: ServerType) -> VersionCatalog:
        catalog = self.by_type.get(server_type)
        if catalog is None:
            raise UnknownServerTypeError(server_type.value)
        return catalog
