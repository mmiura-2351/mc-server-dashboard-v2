"""CRUD use cases for servers (Section 6.5).

These run *after* the route's two-layer authorization dependency has admitted the
caller (non-member -> 404, member-without-permission -> 403; Section 6.4), so they
assume an authorized member and only do the data work.

- :class:`CreateServer` validates the server type and execution backend against
  the known enums and stages a stopped server (desired=stopped, observed=stopped
  per DATABASE.md Section 7) with a fresh id.
- :class:`ReadServer` / :class:`ListServers` are community-scoped reads; a server
  whose ``community_id`` does not match the path community is reported as
  not-found (no cross-community existence signal, FR-COMM-3).
- :class:`UpdateServer` edits name/config only while the server is at rest
  (Section 6.9 spirit); changing the backend is rejected as immutable (FR-EXE-3).
- :class:`DeleteServer` deletes a stopped server and sweeps its resource grants in
  the same transaction (Section 10).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    ExecutionBackendImmutableError,
    ServerNameAlreadyExistsError,
    ServerNotFoundError,
    ServerNotStoppedError,
    UnknownExecutionBackendError,
    UnknownServerTypeError,
)
from mc_server_dashboard_api.servers.domain.snapshot_cadence import (
    override_from_config,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from mc_server_dashboard_api.servers.domain.version_validator import VersionValidator

# The resource type a server grant is keyed by (DATABASE.md Sections 6, 10). The
# delete sweep removes grants for ``(resource_type='server', resource_id=<id>)``.
_SERVER_RESOURCE_TYPE = "server"


def _parse_server_type(value: str) -> ServerType:
    try:
        return ServerType(value)
    except ValueError as exc:
        raise UnknownServerTypeError(value) from exc


def _parse_execution_backend(value: str) -> ExecutionBackend:
    try:
        return ExecutionBackend(value)
    except ValueError as exc:
        raise UnknownExecutionBackendError(value) from exc


@dataclass(frozen=True)
class CreateServer:
    """Create a server within a community (server:create, FR-SRV-1).

    Create validates the requested ``(server_type, mc_version)`` against the global
    version catalog (cheap, no download — the JAR is fetched on first start, the
    ensure-on-start ruling). The check rejects an unsupported type (forge at M1)
    and an unoffered version before the row is staged (FR-VER-1).
    """

    uow: UnitOfWork
    clock: Clock
    version_validator: VersionValidator

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        name: str,
        mc_edition: str,
        mc_version: str,
        server_type: str,
        execution_backend: str,
        config: dict[str, Any],
    ) -> Server:
        parsed_type = _parse_server_type(server_type)
        parsed_backend = _parse_execution_backend(execution_backend)
        await self.version_validator.validate(
            server_type=server_type, version=mc_version
        )
        now = self.clock.now()
        server = Server(
            id=ServerId.new(),
            community_id=community_id,
            name=ServerName(name),
            mc_edition=mc_edition,
            mc_version=mc_version,
            server_type=parsed_type,
            execution_backend=parsed_backend,
            config=config,
            # A new server is at rest: the operator has not asked it to run, and
            # no Worker has reported on it (DATABASE.md Section 7).
            desired_state=DesiredState.STOPPED,
            observed_state=ObservedState.STOPPED,
            observed_at=None,
            assigned_worker_id=None,
            created_at=now,
            updated_at=now,
        )
        async with self.uow:
            await self.uow.servers.add(server)
            await self.uow.commit()
        return server


@dataclass(frozen=True)
class ReadServer:
    """Return a server by id, scoped to its community (server:read)."""

    uow: UnitOfWork

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> Server:
        async with self.uow:
            server = await self.uow.servers.get_by_id(server_id)
        if server is None or server.community_id != community_id:
            raise ServerNotFoundError(str(server_id.value))
        return server


@dataclass(frozen=True)
class ListServers:
    """List the servers in a community (server:read)."""

    uow: UnitOfWork

    async def __call__(self, *, community_id: CommunityId) -> list[Server]:
        async with self.uow:
            return await self.uow.servers.list_for_community(community_id)


@dataclass(frozen=True)
class UpdateServer:
    """Edit a server's name/config while it is at rest (server:update).

    A per-server snapshot-interval override carried on ``config`` is validated
    against ``min_interval_seconds`` (the thrash floor, CONFIGURATION.md Section
    5.4): a below-floor or non-integer value is rejected (FR-DATA-7), surfaced as
    422 at the edge.
    """

    uow: UnitOfWork
    clock: Clock
    min_interval_seconds: int = 0

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        execution_backend: str | None = None,
    ) -> Server:
        new_name = None if name is None else ServerName(name)
        if config is not None:
            # Validate the override before any write; raises on a bad value.
            override_from_config(config, floor=self.min_interval_seconds)
        async with self.uow:
            server = await self.uow.servers.get_by_id(server_id)
            if server is None or server.community_id != community_id:
                raise ServerNotFoundError(str(server_id.value))
            if execution_backend is not None and (
                _parse_execution_backend(execution_backend)
                is not server.execution_backend
            ):
                # The backend is immutable for the server's lifetime (FR-EXE-3).
                raise ExecutionBackendImmutableError(execution_backend)
            if not server.is_at_rest():
                raise ServerNotStoppedError(str(server_id.value))
            if new_name is not None and new_name != server.name:
                clash = await self.uow.servers.get_by_community_and_name(
                    community_id, new_name
                )
                if clash is not None and clash.id != server_id:
                    raise ServerNameAlreadyExistsError(new_name.value)
                server.name = new_name
            if config is not None:
                server.config = config
            server.updated_at = self.clock.now()
            await self.uow.servers.update(server)
            await self.uow.commit()
        return server


@dataclass(frozen=True)
class DeleteServer:
    """Delete a stopped server and sweep its resource grants (server:delete)."""

    uow: UnitOfWork

    async def __call__(self, *, community_id: CommunityId, server_id: ServerId) -> None:
        async with self.uow:
            server = await self.uow.servers.get_by_id(server_id)
            if server is None or server.community_id != community_id:
                raise ServerNotFoundError(str(server_id.value))
            if not server.is_at_rest():
                raise ServerNotStoppedError(str(server_id.value))
            await self.uow.servers.delete(server_id)
            # No FK on resource_grant.resource_id, so the server delete does not
            # cascade; sweep the grants in the same transaction (Section 10).
            await self.uow.resource_grants.delete_for_resource(
                _SERVER_RESOURCE_TYPE, server_id.value
            )
            await self.uow.commit()
