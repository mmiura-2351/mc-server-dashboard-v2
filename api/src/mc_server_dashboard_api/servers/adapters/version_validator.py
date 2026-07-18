"""Catalog-backed adapter for the servers :class:`VersionValidator` seam.

Binds the create-path version-validation Port to the global version catalog
(an adapter-layer composition across bounded contexts, the servers->fleet
precedent). It maps the servers ``server_type`` string onto the versions
``ServerType`` enum: a value the versions enum does not carry is the
unsupported case (the DB CHECK enum could permit a type the catalog does not).
A catalogued type whose version the catalog does not list is the
unknown-version case. ``forge`` is now catalogued (issue #307), so it validates
against the catalog like the others.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.version_validator import (
    CatalogUnavailableError,
    UnknownVersionError,
    UnsupportedServerTypeError,
    VersionValidator,
)
from mc_server_dashboard_api.versions.domain.catalog import VersionCatalog
from mc_server_dashboard_api.versions.domain.errors import (
    CatalogUnavailableError as VersionsCatalogUnavailableError,
)
from mc_server_dashboard_api.versions.domain.errors import (
    UnknownVersionError as VersionsUnknownVersionError,
)
from mc_server_dashboard_api.versions.domain.value_objects import ServerType


@dataclass(frozen=True)
class CatalogVersionValidator(VersionValidator):
    """Validate ``(server_type, version)`` against the global version catalog."""

    catalog: VersionCatalog

    async def validate(self, *, server_type: str, version: str) -> None:
        try:
            catalog_type = ServerType(server_type)
        except ValueError as exc:
            # Valid in the schema CHECK enum but not catalogued (defensive: every
            # current schema type is catalogued, forge included).
            raise UnsupportedServerTypeError(server_type) from exc
        try:
            offered = await self.catalog.list_versions(catalog_type)
        except VersionsCatalogUnavailableError as exc:
            # A transient source outage with no usable cache: translate the
            # versions-domain error into the servers-domain one so the create edge
            # maps it to a 503 without importing the versions domain (FR-VER-2).
            raise CatalogUnavailableError(str(exc)) from exc
        except VersionsUnknownVersionError as exc:
            # A malformed upstream payload (e.g. HTML error page on a 200):
            # the catalog source is unusable, not "version not found" — the
            # membership check is local (issue #1991).
            raise CatalogUnavailableError(str(exc)) from exc
        if version not in {ref.version for ref in offered}:
            raise UnknownVersionError(f"{server_type} {version}")
