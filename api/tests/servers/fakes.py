"""In-memory fakes for the servers Ports used by the use-case tests.

Keeps the use cases under test against fakes (no database), per TESTING.md
Section 4. The fake UnitOfWork shares its repositories across nested ``async
with`` blocks, tracks commits, and records grant sweeps so tests can assert the
server-delete-plus-grant-sweep atomicity (DATABASE.md Section 10).
"""

from __future__ import annotations

import datetime as dt
import io
import uuid
import zipfile
from collections.abc import AsyncIterator
from dataclasses import replace

from mc_server_dashboard_api.servers.domain.backup import Backup, BackupId
from mc_server_dashboard_api.servers.domain.backup_repository import (
    BackupRepository,
)
from mc_server_dashboard_api.servers.domain.backup_store import BackupArchiveStore
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
    ControlPlane,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    BackupNotFoundError,
    ServerFileNotFoundError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileEntry, FileStore
from mc_server_dashboard_api.servers.domain.jar_provisioner import (
    JarProvisioner,
    JarProvisioningError,
)
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
from mc_server_dashboard_api.servers.domain.version_validator import (
    SpigotUnsupportedError,
    UnknownVersionError,
    UnsupportedServerTypeError,
    VersionValidator,
)


class FakeJarProvisioner(JarProvisioner):
    """Start-path JAR provisioning double.

    Returns a fixed content key by default; pass ``fail=True`` to raise
    :class:`JarProvisioningError` (the download/verify-failure path). Records each
    ensure call so a test can assert it ran before placement.
    """

    def __init__(self, *, key: str = "f" * 64, fail: bool = False) -> None:
        self._key = key
        self._fail = fail
        self.calls: list[tuple[str, str, str | None]] = []

    async def ensure(
        self, *, server_type: str, version: str, known_key: str | None
    ) -> str:
        self.calls.append((server_type, version, known_key))
        if self._fail:
            raise JarProvisioningError("forced provisioning failure")
        return self._key


class FakeVersionValidator(VersionValidator):
    """Catalog seam double for create tests.

    Accepts any ``(server_type, version)`` by default; pass ``offered`` to restrict
    the accepted versions per type, or ``unsupported`` to mark a type unsupported
    (the forge case). ``spigot`` is always rejected with
    :class:`SpigotUnsupportedError`, mirroring the real adapter (no official
    distribution API). Anything outside the offered set raises the matching domain
    error, mirroring the real catalog-backed adapter.
    """

    def __init__(
        self,
        *,
        offered: dict[str, set[str]] | None = None,
        unsupported: set[str] | None = None,
    ) -> None:
        self._offered = offered
        self._unsupported = unsupported or set()
        self.calls: list[tuple[str, str]] = []

    async def validate(self, *, server_type: str, version: str) -> None:
        self.calls.append((server_type, version))
        if server_type == "spigot":
            raise SpigotUnsupportedError(f"use paper instead of spigot ({version})")
        if server_type in self._unsupported:
            raise UnsupportedServerTypeError(server_type)
        if self._offered is None:
            return
        if version not in self._offered.get(server_type, set()):
            raise UnknownVersionError(f"{server_type} {version}")


class FakeFileStore(FileStore):
    """In-memory authoritative-copy file store keyed by rel_path.

    Backs the create-seeding tests: ``write_file`` records each seed write so a
    test can assert what landed in the initial working set, and ``read_file``
    serves it back (404 → :class:`ServerFileNotFoundError` for an unseeded path).
    """

    def __init__(self, *, fail_write: bool = False) -> None:
        self.files: dict[str, bytes] = {}
        self.writes: list[tuple[str, bytes]] = []
        # When set, write_file raises to exercise the create seed-failure path
        # (issue #243): the committed row stays, surfaced as a mapped 503.
        self._fail_write = fail_write

    def validate_rel_path(self, rel_path: str) -> None:
        return None

    async def read_file(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> bytes:
        if rel_path not in self.files:
            raise ServerFileNotFoundError(str(server_id.value))
        return self.files[rel_path]

    async def list_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> list[FileEntry]:
        return []

    async def write_file(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        content: bytes,
    ) -> None:
        if self._fail_write:
            raise RuntimeError("forced storage write failure")
        self.files[rel_path] = content
        self.writes.append((rel_path, content))

    async def delete_file(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> None:
        self.files.pop(rel_path, None)

    async def delete_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> None:
        return None

    async def make_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> None:
        return None

    def download_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            return
            yield b""  # pragma: no cover - empty async generator

        return _gen()

    def export_dir(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        extra: list[tuple[str, bytes]],
    ) -> AsyncIterator[bytes]:
        # Build a real zip of every seeded file plus the ``extra`` entries so a
        # round-trip test can re-open and compare the bytes (issue #274).
        files = dict(self.files)

        async def _gen() -> AsyncIterator[bytes]:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, mode="w") as zf:
                for path, content in files.items():
                    zf.writestr(path, content)
                for arcname, content in extra:
                    zf.writestr(arcname, content)
            yield buf.getvalue()

        return _gen()

    async def list_versions(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> list[str]:
        return []

    async def rollback(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        version_id: str,
    ) -> None:
        return None


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

    async def list_game_ports(self) -> set[int]:
        return {s.game_port for s in self.by_id.values() if s.game_port is not None}

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
        *,
        unassign: bool = False,
    ) -> None:
        server = self.by_id.get(server_id)
        if server is None:
            return
        # Mirror the real adapter's monotonic guard (issue #216): drop a write
        # stamped no later than the row's current observed_at; a never-observed row
        # (observed_at is None) still accepts its first write.
        if server.observed_at is not None and observed_at <= server.observed_at:
            return
        server.observed_state = observed_state
        server.observed_at = observed_at
        if unassign:
            server.assigned_worker_id = None

    async def mark_worker_servers_unknown(
        self, worker_id: WorkerId, observed_at: dt.datetime
    ) -> None:
        for server in self.by_id.values():
            if server.assigned_worker_id == worker_id:
                server.observed_state = ObservedState.UNKNOWN
                server.observed_at = observed_at

    async def reset_unverifiable_observed_states(self, observed_at: dt.datetime) -> int:
        non_terminal = (
            ObservedState.STARTING,
            ObservedState.RUNNING,
            ObservedState.STOPPING,
            ObservedState.RESTARTING,
        )
        count = 0
        for server in self.by_id.values():
            if (
                server.assigned_worker_id is not None
                and server.observed_state in non_terminal
            ):
                server.observed_state = ObservedState.UNKNOWN
                server.observed_at = observed_at
                count += 1
        return count

    async def count_running_for_worker(self, worker_id: WorkerId) -> int:
        return sum(
            1
            for server in self.by_id.values()
            if server.assigned_worker_id == worker_id
            and server.desired_state is DesiredState.RUNNING
        )

    async def list_running_assigned(self) -> list[Server]:
        return [
            replace(server)
            for server in self.by_id.values()
            if server.desired_state is DesiredState.RUNNING
            and server.assigned_worker_id is not None
        ]

    async def list_all(self) -> list[Server]:
        return [replace(server) for server in self.by_id.values()]

    async def list_reconcilable(self) -> list[Server]:
        out: list[Server] = []
        for server in self.by_id.values():
            running = server.desired_state is DesiredState.RUNNING
            stopped = server.desired_state is DesiredState.STOPPED
            stale_running = running and server.observed_state not in (
                ObservedState.STARTING,
                ObservedState.RUNNING,
            )
            orphan = running and server.assigned_worker_id is None
            stop_undelivered = (
                stopped and server.observed_state is ObservedState.RUNNING
            )
            if stale_running or orphan or stop_undelivered:
                out.append(replace(server))
        return out

    async def delete(self, server_id: ServerId) -> None:
        self.by_id.pop(server_id, None)


class FakeResourceGrantSweeper(ResourceGrantSweeper):
    def __init__(self) -> None:
        self.swept: list[tuple[str, uuid.UUID]] = []

    async def delete_for_resource(
        self, resource_type: str, resource_id: uuid.UUID
    ) -> None:
        self.swept.append((resource_type, resource_id))


class FakeBackupRepository(BackupRepository):
    def __init__(self) -> None:
        self.by_id: dict[BackupId, Backup] = {}

    def seed(self, backup: Backup) -> None:
        self.by_id[backup.id] = backup

    async def add(self, backup: Backup) -> None:
        self.by_id[backup.id] = backup

    async def get_by_id(self, backup_id: BackupId) -> Backup | None:
        backup = self.by_id.get(backup_id)
        return None if backup is None else replace(backup)

    async def list_for_server(self, server_id: ServerId) -> list[Backup]:
        rows = [replace(b) for b in self.by_id.values() if b.server_id == server_id]
        return sorted(rows, key=lambda b: b.created_at, reverse=True)

    async def delete(self, backup_id: BackupId) -> None:
        self.by_id.pop(backup_id, None)


class FakeUnitOfWork(UnitOfWork):
    # Narrow the Port-declared attribute types to the concrete fakes so tests can
    # reach their inspection helpers without casts.
    servers: FakeServerRepository
    resource_grants: FakeResourceGrantSweeper
    backups: FakeBackupRepository

    def __init__(
        self,
        servers: FakeServerRepository | None = None,
        resource_grants: FakeResourceGrantSweeper | None = None,
        backups: FakeBackupRepository | None = None,
    ) -> None:
        self.servers = servers or FakeServerRepository()
        self.resource_grants = resource_grants or FakeResourceGrantSweeper()
        self.backups = backups or FakeBackupRepository()
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
        outcomes: dict[str, CommandOutcome] | None = None,
        raise_unavailable: bool = False,
        unavailable_kinds: set[str] | None = None,
        connected: dict[WorkerId, bool] | None = None,
    ) -> None:
        self._place_to = place_to
        self._outcome = outcome or CommandOutcome(status=CommandStatus.OK)
        # Per-kind overrides let a test fail one dispatch (e.g. start) while another
        # succeeds (e.g. hydrate); kinds absent here fall back to ``outcome``.
        self._outcomes = outcomes or {}
        self._raise_unavailable = raise_unavailable
        # Per-kind WorkerUnavailableError: a test can make only ``start`` raise
        # (a timeout/lost response) while ``hydrate`` succeeds, exercising the
        # post-dispatch stickiness path (issue #101).
        self._unavailable_kinds = unavailable_kinds or set()
        # Worker-connectivity map for the scheduler's skip-disconnected path; a
        # worker absent here is treated as connected.
        self._connected = connected or {}
        self.dispatched: list[tuple[str, WorkerId, ServerId]] = []
        self.incremented: list[WorkerId] = []
        self.decremented: list[WorkerId] = []
        # The ``force`` flag of the last stop dispatch, so a test can assert the
        # use case forwarded the caller's choice (issue #270).
        self.stop_force: bool | None = None

    async def place(self, *, backend: ExecutionBackend) -> WorkerId | None:
        return self._place_to

    def is_worker_connected(self, *, worker_id: WorkerId) -> bool:
        return self._connected.get(worker_id, True)

    def increment_assignment(self, *, worker_id: WorkerId) -> None:
        self.incremented.append(worker_id)

    def decrement_assignment(self, *, worker_id: WorkerId) -> None:
        self.decremented.append(worker_id)

    async def _record(
        self, kind: str, worker_id: WorkerId, server_id: ServerId
    ) -> CommandOutcome:
        if self._raise_unavailable or kind in self._unavailable_kinds:
            from mc_server_dashboard_api.servers.domain.control_plane import (
                WorkerUnavailableError,
            )

            # Record the per-kind raise so a test can assert the dispatch was
            # attempted (e.g. start sent, response lost); the global flag keeps its
            # original no-record behavior.
            if kind in self._unavailable_kinds:
                self.dispatched.append((kind, worker_id, server_id))
            raise WorkerUnavailableError(str(worker_id.value))
        self.dispatched.append((kind, worker_id, server_id))
        return self._outcomes.get(kind, self._outcome)

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
        self.stop_force = force
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

    async def read_file(
        self, *, worker_id: WorkerId, server_id: ServerId, rel_path: str
    ) -> CommandOutcome:
        return await self._record("read_file", worker_id, server_id)

    async def edit_file(
        self,
        *,
        worker_id: WorkerId,
        server_id: ServerId,
        rel_path: str,
        content: bytes,
    ) -> CommandOutcome:
        return await self._record("edit_file", worker_id, server_id)

    async def list_files(
        self, *, worker_id: WorkerId, server_id: ServerId, rel_path: str
    ) -> CommandOutcome:
        return await self._record("list_files", worker_id, server_id)


class FakeBackupArchiveStore(BackupArchiveStore):
    """In-memory backup-archive seam for the backup use-case tests.

    Records every operation and tracks the archives that "exist" so a test can
    assert create -> ref, restore-of-known-ref, idempotent delete, and the
    delete-ordering (archive removed before the metadata row). ``create_from_current``
    mints a fresh ref; ``missing`` makes the next create raise the
    no-working-set error.
    """

    def __init__(self, *, missing: bool = False) -> None:
        self._missing = missing
        self.archives: set[str] = set()
        self.created: list[ServerId] = []
        self.restored: list[tuple[ServerId, str]] = []
        self.deleted: list[tuple[ServerId, str]] = []
        self._counter = 0

    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> str:
        if self._missing:
            raise BackupNotFoundError(str(server_id.value))
        self._counter += 1
        ref = f"archive-{self._counter}"
        self.archives.add(ref)
        self.created.append(server_id)
        return ref

    async def restore(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        if storage_ref not in self.archives:
            raise BackupNotFoundError(storage_ref)
        self.restored.append((server_id, storage_ref))

    async def delete(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        self.archives.discard(storage_ref)
        self.deleted.append((server_id, storage_ref))
