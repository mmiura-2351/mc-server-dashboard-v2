"""In-memory fakes for the servers Ports used by the use-case tests.

Keeps the use cases under test against fakes (no database), per TESTING.md
Section 4. The fake UnitOfWork shares its repositories across nested ``async
with`` blocks, tracks commits, and records grant sweeps so tests can assert the
server-delete-plus-grant-sweep atomicity (DATABASE.md Section 10).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import replace

from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
    ControlPlane,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.repositories import (
    ResourceGrantSweeper,
    ServerRepository,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    WorkerId,
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
        # Return a detached copy so a use case that mutates the loaded entity
        # before writing does not silently mutate the "persisted" row; this lets
        # update_lifecycle compare against the actual stored desired state (the
        # compare-and-set the real adapter does in SQL).
        server = self.by_id.get(server_id)
        return None if server is None else replace(server)

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

    async def update_lifecycle(
        self,
        server: Server,
        *,
        expected_from: DesiredState,
        require_unassigned: bool = False,
    ) -> bool:
        current = self.by_id.get(server.id)
        if current is None or current.desired_state is not expected_from:
            return False
        if require_unassigned and current.assigned_worker_id is not None:
            return False
        self.by_id[server.id] = server
        return True

    async def record_observed_state(
        self,
        server_id: ServerId,
        observed_state: ObservedState,
        observed_at: dt.datetime,
    ) -> None:
        server = self.by_id.get(server_id)
        if server is not None:
            server.observed_state = observed_state
            server.observed_at = observed_at

    async def mark_worker_servers_unknown(
        self, worker_id: WorkerId, observed_at: dt.datetime
    ) -> None:
        for server in self.by_id.values():
            if server.assigned_worker_id == worker_id:
                server.observed_state = ObservedState.UNKNOWN
                server.observed_at = observed_at

    async def count_running_for_worker(self, worker_id: WorkerId) -> int:
        return sum(
            1
            for server in self.by_id.values()
            if server.assigned_worker_id == worker_id
            and server.desired_state is DesiredState.RUNNING
        )

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


class FakeControlPlane(ControlPlane):
    """In-memory control-plane seam for the lifecycle use-case tests.

    Records every dispatch and assignment mutation, and returns configurable
    placement / command outcomes so a test can drive the happy path, the
    no-eligible-worker path, and the dispatch-failure compensation path.
    """

    def __init__(
        self,
        *,
        place_to: WorkerId | None = None,
        outcome: CommandOutcome | None = None,
        raise_unavailable: bool = False,
    ) -> None:
        self._place_to = place_to
        self._outcome = outcome or CommandOutcome(status=CommandStatus.OK)
        self._raise_unavailable = raise_unavailable
        self.dispatched: list[tuple[str, WorkerId, ServerId]] = []
        self.incremented: list[WorkerId] = []
        self.decremented: list[WorkerId] = []

    async def place(self, *, backend: ExecutionBackend) -> WorkerId | None:
        return self._place_to

    def increment_assignment(self, *, worker_id: WorkerId) -> None:
        self.incremented.append(worker_id)

    def decrement_assignment(self, *, worker_id: WorkerId) -> None:
        self.decremented.append(worker_id)

    async def _record(
        self, kind: str, worker_id: WorkerId, server_id: ServerId
    ) -> CommandOutcome:
        if self._raise_unavailable:
            from mc_server_dashboard_api.servers.domain.control_plane import (
                WorkerUnavailableError,
            )

            raise WorkerUnavailableError(str(worker_id.value))
        self.dispatched.append((kind, worker_id, server_id))
        return self._outcome

    async def start(
        self,
        *,
        worker_id: WorkerId,
        server_id: ServerId,
        backend: ExecutionBackend,
        jar_relpath: str,
        minecraft_version: str,
    ) -> CommandOutcome:
        return await self._record("start", worker_id, server_id)

    async def stop(
        self, *, worker_id: WorkerId, server_id: ServerId, force: bool = False
    ) -> CommandOutcome:
        return await self._record("stop", worker_id, server_id)

    async def restart(
        self, *, worker_id: WorkerId, server_id: ServerId
    ) -> CommandOutcome:
        return await self._record("restart", worker_id, server_id)

    async def command(
        self, *, worker_id: WorkerId, server_id: ServerId, line: str
    ) -> CommandOutcome:
        return await self._record("command", worker_id, server_id)

    async def hydrate(
        self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
    ) -> CommandOutcome:
        return await self._record("hydrate", worker_id, server_id)

    async def snapshot(
        self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
    ) -> CommandOutcome:
        return await self._record("snapshot", worker_id, server_id)
