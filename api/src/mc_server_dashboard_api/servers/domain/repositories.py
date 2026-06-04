"""Persistence Ports for the servers context.

The ``ServerRepository`` interface (ARCHITECTURE.md Section 5.1) the domain
depends on; a concrete async-SQLAlchemy adapter implements it. Lookups return
``None`` when absent rather than raising, so callers decide policy.

:class:`ResourceGrantSweeper` is a narrow Port for the server-delete grant sweep
(DATABASE.md Section 10): ``resource_grant`` rows carry no FK on ``resource_id``,
so deleting a server does not cascade to its grants. The sweep is owned by the
community context, but the servers use case must not import another context's
domain (import-linter). This Port is the clean seam: the wiring binds it to the
community resource-grant adapter on the *same* session as the server delete, so
both run in one transaction.
"""

from __future__ import annotations

import abc
import uuid

from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
    ServerName,
)


class ServerRepository(abc.ABC):
    """Port: persistence for :class:`Server` aggregates."""

    @abc.abstractmethod
    async def add(self, server: Server) -> None:
        """Stage a new server for persistence within the current transaction."""

    @abc.abstractmethod
    async def get_by_id(self, server_id: ServerId) -> Server | None:
        """Return the server with ``server_id``, or ``None`` if absent."""

    @abc.abstractmethod
    async def get_by_community_and_name(
        self, community_id: CommunityId, name: ServerName
    ) -> Server | None:
        """Return the server named ``name`` in ``community_id``, or ``None``."""

    @abc.abstractmethod
    async def list_for_community(self, community_id: CommunityId) -> list[Server]:
        """Return all servers in ``community_id`` (the ``server:read`` listing)."""

    @abc.abstractmethod
    async def update(self, server: Server) -> None:
        """Persist the mutable fields of ``server`` (name, config, timestamps)."""

    @abc.abstractmethod
    async def delete(self, server_id: ServerId) -> None:
        """Delete the server row (its grants are swept separately, Section 10)."""


class ResourceGrantSweeper(abc.ABC):
    """Port: delete all resource grants on a specific resource (Section 10)."""

    @abc.abstractmethod
    async def delete_for_resource(
        self, resource_type: str, resource_id: uuid.UUID
    ) -> None:
        """Delete all grants on ``(resource_type, resource_id)``.

        Called by the server-delete use case in the same transaction as the
        server-row delete, since grants FK nothing on ``resource_id``.
        """
