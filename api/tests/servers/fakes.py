"""In-memory fakes for the servers Ports used by the use-case tests.

Keeps the use cases under test against fakes (no database), per TESTING.md
Section 4. The fake UnitOfWork shares its repositories across nested ``async
with`` blocks, tracks commits, and records grant sweeps so tests can assert the
server-delete-plus-grant-sweep atomicity (DATABASE.md Section 10).
"""

from __future__ import annotations

import datetime as dt
import uuid

from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.repositories import (
    ResourceGrantSweeper,
    ServerRepository,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
    ServerName,
)


class FakeClock(Clock):
    def __init__(self, now: dt.datetime) -> None:
        self._now = now

    def set(self, now: dt.datetime) -> None:
        self._now = now

    def now(self) -> dt.datetime:
        return self._now


class FakeServerRepository(ServerRepository):
    def __init__(self) -> None:
        self.by_id: dict[ServerId, Server] = {}

    def seed(self, server: Server) -> None:
        self.by_id[server.id] = server

    async def add(self, server: Server) -> None:
        self.by_id[server.id] = server

    async def get_by_id(self, server_id: ServerId) -> Server | None:
        return self.by_id.get(server_id)

    async def get_by_community_and_name(
        self, community_id: CommunityId, name: ServerName
    ) -> Server | None:
        for server in self.by_id.values():
            if server.community_id == community_id and server.name == name:
                return server
        return None

    async def list_for_community(self, community_id: CommunityId) -> list[Server]:
        return [s for s in self.by_id.values() if s.community_id == community_id]

    async def update(self, server: Server) -> None:
        self.by_id[server.id] = server

    async def delete(self, server_id: ServerId) -> None:
        self.by_id.pop(server_id, None)


class FakeResourceGrantSweeper(ResourceGrantSweeper):
    def __init__(self) -> None:
        self.swept: list[tuple[str, uuid.UUID]] = []

    async def delete_for_resource(
        self, resource_type: str, resource_id: uuid.UUID
    ) -> None:
        self.swept.append((resource_type, resource_id))


class FakeUnitOfWork(UnitOfWork):
    # Narrow the Port-declared attribute types to the concrete fakes so tests can
    # reach their inspection helpers without casts.
    servers: FakeServerRepository
    resource_grants: FakeResourceGrantSweeper

    def __init__(
        self,
        servers: FakeServerRepository | None = None,
        resource_grants: FakeResourceGrantSweeper | None = None,
    ) -> None:
        self.servers = servers or FakeServerRepository()
        self.resource_grants = resource_grants or FakeResourceGrantSweeper()
        self.commits = 0

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None
