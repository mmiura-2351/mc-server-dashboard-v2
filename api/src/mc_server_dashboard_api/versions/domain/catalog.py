"""The ``VersionCatalog`` Port (ARCHITECTURE.md Section 5.1, FR-VER-1/2).

The API-side seam that lists supported MC versions/types and resolves a
downloadable JAR descriptor for a ``(server_type, version)`` pair. It is global —
the catalog carries no community/server scope (STORAGE.md Section 8.1: JARs are
shared platform-wide). Concrete adapters talk to external manifests (the Mojang
version manifest, the PaperMC API) behind retry + cache fallback (FR-VER-2); the
wiring binds them in the composition root.

The Port returns a :class:`~.value_objects.JarSource` (URL + expected hash); it
never downloads or stores bytes itself — fetching + content-addressed persistence
through ``Storage`` is the ensure-on-start use case's job (FR-VER-3).
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.versions.domain.value_objects import (
    JarSource,
    ServerType,
    VersionRef,
)


class VersionCatalog(abc.ABC):
    """Port: list MC versions/types and resolve a JAR source (FR-VER-1)."""

    @abc.abstractmethod
    async def list_versions(self, server_type: ServerType) -> list[VersionRef]:
        """List the versions offered for ``server_type``, newest-first.

        Raises :class:`~.errors.CatalogUnavailableError` if the source is down and
        no cached payload can serve the listing (FR-VER-2).
        """

    @abc.abstractmethod
    async def resolve(self, server_type: ServerType, version: str) -> JarSource:
        """Resolve the downloadable JAR for ``(server_type, version)``.

        Raises :class:`~.errors.UnknownVersionError` for a version the source does
        not offer, and :class:`~.errors.CatalogUnavailableError` if the source is
        unreachable with no cache to fall back on (FR-VER-2).
        """
