"""Catalog-backed adapter for the servers :class:`VersionValidator` seam.

Binds the create-path version-validation Port to the global version catalog
(an adapter-layer composition across bounded contexts, the servers->fleet
precedent). It maps the servers ``server_type`` string onto the versions
``ServerType`` enum: a value the versions enum does not carry (``forge``) is the
M1-unsupported case (the DB CHECK enum still permits it, the catalog does not).
A catalogued type whose version the catalog does not list is the unknown-version
case.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.version_validator import (
    UnknownVersionError,
    UnsupportedServerTypeError,
    VersionValidator,
)
from mc_server_dashboard_api.versions.domain.catalog import VersionCatalog
from mc_server_dashboard_api.versions.domain.value_objects import ServerType


@dataclass(frozen=True)
class CatalogVersionValidator(VersionValidator):
    """Validate ``(server_type, version)`` against the global version catalog."""

    catalog: VersionCatalog

    async def validate(self, *, server_type: str, version: str) -> None:
        try:
            catalog_type = ServerType(server_type)
        except ValueError as exc:
            # Valid in the schema CHECK enum (e.g. forge) but not catalogued at M1.
            raise UnsupportedServerTypeError(server_type) from exc
        offered = await self.catalog.list_versions(catalog_type)
        if version not in {ref.version for ref in offered}:
            raise UnknownVersionError(f"{server_type} {version}")
