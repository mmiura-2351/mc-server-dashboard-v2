"""Persistence Port for resource pack metadata.

The ``ResourcePackRepository`` the resource-pack use cases depend on; a concrete
async-SQLAlchemy adapter implements it on the unit-of-work's session. Lookups
return ``None`` when absent rather than raising, so callers decide policy
(mirroring :class:`BackupRepository`).
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.servers.domain.resource_pack import (
    ResourcePack,
    ResourcePackAssignment,
    ResourcePackId,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


class ResourcePackRepository(abc.ABC):
    """Port: persistence for resource packs and their assignments."""

    @abc.abstractmethod
    async def add(self, pack: ResourcePack) -> None:
        """Stage a new resource pack row for persistence."""

    @abc.abstractmethod
    async def get_by_id(self, pack_id: ResourcePackId) -> ResourcePack | None:
        """Return the resource pack with ``pack_id``, or ``None`` if absent."""

    @abc.abstractmethod
    async def list_all(self) -> list[ResourcePack]:
        """Return all resource packs ordered by display_name."""

    @abc.abstractmethod
    async def delete(self, pack_id: ResourcePackId) -> None:
        """Delete the resource pack row; the DELETE runs inside this call.

        Not a staged write: the assignment foreign key is not ``DEFERRABLE``, so
        a pack an assignment still references is refused at statement end rather
        than at the unit of work's commit, and this call raises
        :class:`ResourcePackInUseError` (issue #2612).
        """

    @abc.abstractmethod
    async def add_assignment(self, assignment: ResourcePackAssignment) -> None:
        """Insert the assignment row; the INSERT runs inside this call.

        Not a staged write: the row's foreign keys are enforced here rather than
        at the unit of work's commit, so this call raises
        :class:`ResourcePackNotFoundError` when the pack it names is gone (issue
        #2784) and :class:`ServerNotFoundError` when the server is (issue #2852).

        ``server_id`` is the primary key, so this is always an INSERT and never
        an upsert: a server that already has an assignment duplicates the key,
        which no translation covers and which therefore surfaces as a raw
        ``IntegrityError`` (issue #2858); callers replacing an assignment delete
        the existing row first.
        """

    @abc.abstractmethod
    async def get_assignment_by_server(
        self, server_id: ServerId
    ) -> ResourcePackAssignment | None:
        """Return the assignment for ``server_id``, or ``None`` if absent."""

    @abc.abstractmethod
    async def delete_assignment(self, server_id: ServerId) -> None:
        """Delete the assignment for ``server_id``."""

    @abc.abstractmethod
    async def list_assignments_for_pack(
        self, pack_id: ResourcePackId
    ) -> list[ResourcePackAssignment]:
        """Return all assignments referencing ``pack_id``.

        Needed to check if a pack is in use before deletion.
        """
