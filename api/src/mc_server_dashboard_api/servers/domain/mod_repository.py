"""Persistence Port for mod library metadata.

The ``ModRepository`` the mod-library use cases depend on; a concrete
async-SQLAlchemy adapter implements it on the unit-of-work's session. Lookups
return ``None`` when absent rather than raising, so callers decide policy
(mirroring :class:`ResourcePackRepository`). ``get_by_sha256`` backs the
content-address dedup (reject/return existing on identical upload).
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.servers.domain.mod import (
    Mod,
    ModId,
    ModLoader,
    ModSide,
)


class ModRepository(abc.ABC):
    """Port: persistence for global mod library entries."""

    @abc.abstractmethod
    async def add(self, mod: Mod) -> None:
        """Stage a new mod row for persistence."""

    @abc.abstractmethod
    async def get_by_id(self, mod_id: ModId) -> Mod | None:
        """Return the mod with ``mod_id``, or ``None`` if absent."""

    @abc.abstractmethod
    async def get_by_sha256(self, sha256_hash: str) -> Mod | None:
        """Return the mod with ``sha256_hash``, or ``None`` if absent.

        Backs the content-address dedup: an identical upload resolves to the
        existing library entry.
        """

    @abc.abstractmethod
    async def list_all(
        self,
        *,
        loader_type: ModLoader | None = None,
        mc_version: str | None = None,
        side: ModSide | None = None,
    ) -> list[Mod]:
        """Return library mods ordered by display_name, optionally filtered.

        ``loader_type`` and ``side`` filter on the equal column; ``mc_version``
        matches mods listing that version in ``mc_versions``.
        """

    @abc.abstractmethod
    async def delete(self, mod_id: ModId) -> None:
        """Delete the mod row."""
