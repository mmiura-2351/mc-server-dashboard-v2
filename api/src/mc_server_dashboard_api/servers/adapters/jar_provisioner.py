"""Catalog-backed adapter for the servers :class:`JarProvisioner` seam.

Binds the start-path JAR-provisioning Port to the versions :class:`EnsureJar` use
case (an adapter-layer composition across bounded contexts). It maps the servers
``server_type`` string onto the versions ``ServerType`` enum and wraps every
versions-context failure in :class:`JarProvisioningError` so the lifecycle layer
surfaces one typed start failure without importing the versions domain.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.jar_provisioner import (
    JarProvisioner,
    JarProvisioningError,
)
from mc_server_dashboard_api.versions.application.ensure_jar import EnsureJar
from mc_server_dashboard_api.versions.domain.errors import VersionError
from mc_server_dashboard_api.versions.domain.value_objects import ServerType


@dataclass(frozen=True)
class CatalogJarProvisioner(JarProvisioner):
    """Ensure the resolved JAR is pooled via the versions ``EnsureJar`` use case."""

    ensure_jar: EnsureJar

    async def ensure(
        self, *, server_type: str, version: str, known_key: str | None
    ) -> str:
        try:
            catalog_type = ServerType(server_type)
        except ValueError as exc:
            # forge (or any non-catalogued type) cannot be provisioned at M1. Create
            # validation should already have rejected it, so reaching here means a
            # row predating validation; fail the start cleanly.
            raise JarProvisioningError(
                f"{server_type} is not provisionable at M1"
            ) from exc
        try:
            return await self.ensure_jar(
                server_type=catalog_type, version=version, known_key=known_key
            )
        except VersionError as exc:
            raise JarProvisioningError(str(exc)) from exc
