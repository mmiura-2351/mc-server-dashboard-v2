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
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupStatistics,
)
from mc_server_dashboard_api.servers.domain.backup_repository import (
    BackupRepository,
)
from mc_server_dashboard_api.servers.domain.backup_store import BackupArchiveStore
from mc_server_dashboard_api.servers.domain.bedrock_tunnel import BedrockTunnelSync
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogProject,
    CatalogProvider,
    CatalogSearchResponse,
    CatalogSearchResult,
    CatalogVersion,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.committed_resources import (
    CommittedResources,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
    ControlPlane,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    BackupCorruptError,
    BackupNotFoundError,
    ServerFileNotFoundError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileEntry, FileStore
from mc_server_dashboard_api.servers.domain.game_session import GameSession
from mc_server_dashboard_api.servers.domain.game_session_repository import (
    GameSessionRepository,
)
from mc_server_dashboard_api.servers.domain.group_repository import GroupRepository
from mc_server_dashboard_api.servers.domain.groups import (
    GroupId,
    GroupKind,
    GroupName,
    PlayerGroup,
)
from mc_server_dashboard_api.servers.domain.jar_provisioner import (
    JarProvisioner,
    JarProvisioningError,
    ProvisionedJar,
)
from mc_server_dashboard_api.servers.domain.lifecycle_lock import LifecycleLock
from mc_server_dashboard_api.servers.domain.memory_limit import memory_limit_from_config
from mc_server_dashboard_api.servers.domain.notifier import ServerNotifier
from mc_server_dashboard_api.servers.domain.plugin import (
    CATALOG_SOURCES,
    PluginId,
    PluginSource,
    ServerPlugin,
    has_enabled_geyser,
)
from mc_server_dashboard_api.servers.domain.plugin_cache_store import (
    CacheEntry,
    PluginCacheStore,
)
from mc_server_dashboard_api.servers.domain.plugin_repository import PluginRepository
from mc_server_dashboard_api.servers.domain.repositories import (
    ResourceGrantSweeper,
    ServerRepository,
)
from mc_server_dashboard_api.servers.domain.resource_pack import (
    ResourcePack,
    ResourcePackAssignment,
    ResourcePackId,
)
from mc_server_dashboard_api.servers.domain.resource_pack_repository import (
    ResourcePackRepository,
)
from mc_server_dashboard_api.servers.domain.resource_pack_store import (
    ResourcePackStore,
)
from mc_server_dashboard_api.servers.domain.schedule import (
    Schedule,
    ScheduleAction,
    ScheduleId,
    ScheduleRun,
)
from mc_server_dashboard_api.servers.domain.schedule_repository import (
    ScheduleRepository,
    ScheduleRunRepository,
)
from mc_server_dashboard_api.servers.domain.store_generation import (
    StoreGenerationReader,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from mc_server_dashboard_api.servers.domain.version_validator import (
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

    def __init__(
        self,
        *,
        key: str = "f" * 64,
        source: str | None = "sha256:" + "f" * 64,
        fail: bool = False,
    ) -> None:
        self._key = key
        self._source = source
        self._fail = fail
        self.calls: list[tuple[str, str, str | None, str | None]] = []

    async def ensure(
        self,
        *,
        server_type: str,
        version: str,
        known_key: str | None,
        known_source: str | None = None,
    ) -> ProvisionedJar:
        self.calls.append((server_type, version, known_key, known_source))
        if self._fail:
            raise JarProvisioningError("forced provisioning failure")
        return ProvisionedJar(key=self._key, source=self._source)


class FakeStoreGenerationReader(StoreGenerationReader):
    """Authoritative store-generation seam double for the skip-hydrate decision.

    Returns a fixed generation for every server (default 0, the "no snapshot
    published" case). Pass ``generation`` to pin a non-zero authoritative store
    generation (issue #763) so a test can drive the reconciler's
    ``held >= store`` comparison.
    """

    def __init__(self, *, generation: int = 0) -> None:
        self._generation = generation

    async def current_generation(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> int:
        return self._generation


class FakeVersionValidator(VersionValidator):
    """Catalog seam double for create tests.

    Accepts any ``(server_type, version)`` by default; pass ``offered`` to restrict
    the accepted versions per type, or ``unsupported`` to mark a type unsupported
    (the forge case). Anything outside the offered set raises the matching domain
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

    def __init__(self, *, fail_write: bool = False, seed_eula: bool = False) -> None:
        self.files: dict[str, bytes] = {}
        if seed_eula:
            self.files["eula.txt"] = b"eula=true\n"
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

    def open_file_stream(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> AsyncIterator[bytes]:
        files = self.files

        async def _gen() -> AsyncIterator[bytes]:
            if rel_path not in files:
                raise ServerFileNotFoundError(str(server_id.value))
            yield files[rel_path]

        return _gen()

    async def list_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> list[FileEntry]:
        prefix = "" if rel_path == "." else rel_path.rstrip("/") + "/"
        seen: set[str] = set()
        entries: list[FileEntry] = []
        for path, content in self.files.items():
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix) :]
            # Direct child only (no nested slashes).
            if "/" in rest:
                continue
            if rest not in seen:
                seen.add(rest)
                entries.append(FileEntry(name=rest, is_dir=False, size=len(content)))
        return entries

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

    async def retain_if_changed(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> None:
        return None

    async def delete_file(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> None:
        self.files.pop(rel_path, None)

    async def delete_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> None:
        return None

    async def rename_file(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        from_path: str,
        to_path: str,
    ) -> None:
        if from_path not in self.files:
            raise ServerFileNotFoundError(str(server_id.value))
        self.files[to_path] = self.files.pop(from_path)

    async def rename_dir(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        from_path: str,
        to_path: str,
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

    async def read_version(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        version_id: str,
    ) -> bytes:
        raise ServerFileNotFoundError(str(server_id.value))

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

    async def list_bedrock_ports(self) -> set[int]:
        return {
            s.bedrock_port for s in self.by_id.values() if s.bedrock_port is not None
        }

    async def list_slugs(self) -> set[str]:
        return {s.slug for s in self.by_id.values() if s.slug}

    async def get_by_slug(self, slug: str) -> Server | None:
        for server in self.by_id.values():
            if server.slug == slug:
                return server
        return None

    async def list_ids_missing_game_port(self) -> list[ServerId]:
        return [s.id for s in self.by_id.values() if s.game_port is None]

    async def update(self, server: Server) -> None:
        self.by_id[server.id] = server

    async def update_backup_retention(
        self, server_id: ServerId, retention: dict[str, Any] | None
    ) -> None:
        # Mirror the real adapter's narrow single-column write (issue #1841):
        # a missing id matches no row — a harmless no-op.
        server = self.by_id.get(server_id)
        if server is not None:
            server.backup_retention = retention

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
        expected_worker: WorkerId | None = None,
    ) -> bool:
        server = self.by_id.get(server_id)
        if server is None:
            return False
        # Mirror the real adapter's ownership condition (issue #1708): an asserted
        # worker must still be the assigned one, and never matches an unassigned
        # row.
        if expected_worker is not None and server.assigned_worker_id != expected_worker:
            return False
        # Mirror the real adapter's monotonic guard (issue #216): drop a write
        # stamped no later than the row's current observed_at; a never-observed row
        # (observed_at is None) still accepts its first write.
        if server.observed_at is not None and observed_at <= server.observed_at:
            # Mirror the real adapter's applied flag (issue #292, #249 equivalence):
            # a dropped write reports False so the caller keeps its return honest.
            return False
        server.observed_state = observed_state
        server.observed_at = observed_at
        if unassign:
            server.assigned_worker_id = None
        return True

    async def clear_assignment_after_final_snapshot(
        self, server_id: ServerId, worker_id: WorkerId
    ) -> bool:
        # Mirror the real adapter's guard (issue #847): clear only a still
        # desired=stopped row still assigned to worker_id.
        server = self.by_id.get(server_id)
        if (
            server is None
            or server.desired_state is not DesiredState.STOPPED
            or server.assigned_worker_id != worker_id
        ):
            return False
        server.assigned_worker_id = None
        return True

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

    async def running_assignment_ids_for_worker(
        self, worker_id: WorkerId
    ) -> dict[str, int]:
        return {
            str(server.id.value): memory_limit_from_config(server.config) or 0
            for server in self.by_id.values()
            if server.assigned_worker_id == worker_id
            and server.desired_state is DesiredState.RUNNING
        }

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
            # Issue #847 (bug 2): a stop wedged at (stopped, stopped, assigned) when
            # the deferred unassign never ran (crash/cancel mid final-snapshot).
            stop_wedged = (
                stopped
                and server.observed_state is ObservedState.STOPPED
                and server.assigned_worker_id is not None
            )
            # Issue #1599: a stop interrupted mid-flight leaves (stopped, unknown,
            # assigned) — API restart or worker disconnect.
            stop_unknown_wedged = (
                stopped
                and server.observed_state is ObservedState.UNKNOWN
                and server.assigned_worker_id is not None
            )
            if (
                stale_running
                or orphan
                or stop_undelivered
                or stop_wedged
                or stop_unknown_wedged
            ):
                out.append(replace(server))
        return out

    async def existing_ids(self, server_ids: list[ServerId]) -> set[ServerId]:
        return {sid for sid in server_ids if sid in self.by_id}

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

    async def update_health(self, backup_id: BackupId, health: BackupHealth) -> None:
        backup = self.by_id.get(backup_id)
        if backup is not None:
            self.by_id[backup_id] = replace(backup, health=health)

    async def update_size(self, backup_id: BackupId, size_bytes: int) -> None:
        backup = self.by_id.get(backup_id)
        if backup is not None:
            self.by_id[backup_id] = replace(backup, size_bytes=size_bytes)

    async def global_statistics(self) -> BackupStatistics:
        rows = list(self.by_id.values())
        known = [b.size_bytes for b in rows if b.size_bytes is not None]
        times = [b.created_at for b in rows]
        return BackupStatistics(
            count=len(rows),
            total_bytes=sum(known),
            unknown_size_count=len(rows) - len(known),
            newest=max(times) if times else None,
            oldest=min(times) if times else None,
        )


class FakeGameSessionRepository(GameSessionRepository):
    def __init__(self) -> None:
        self.rows: list[GameSession] = []
        self.deleted_before: list[dt.datetime] = []

    def seed(self, session: GameSession) -> None:
        self.rows.append(session)

    async def list_for_server(
        self, server_id: ServerId, *, limit: int, offset: int
    ) -> list[GameSession]:
        matching = [r for r in self.rows if r.server_id == server_id]
        ordered = sorted(
            matching,
            key=lambda r: (r.started_at or dt.datetime.min, str(r.id)),
            reverse=True,
        )
        return ordered[offset : offset + limit]

    async def delete_started_before(self, cutoff: dt.datetime) -> int:
        self.deleted_before.append(cutoff)
        stale = [
            r
            for r in self.rows
            if (r.started_at is not None and r.started_at < cutoff)
            or (r.started_at is None and r.ended_at is not None and r.ended_at < cutoff)
        ]
        self.rows = [r for r in self.rows if r not in stale]
        return len(stale)


class FakeGroupRepository(GroupRepository):
    """In-memory player-group store + attachment join (issue #276).

    Groups are stored by id (returning detached copies so a use case mutating the
    loaded aggregate does not silently mutate the "persisted" one until ``save``);
    attachments are a set of ``(group_id, server_id)`` pairs.
    """

    def __init__(self) -> None:
        self.by_id: dict[GroupId, PlayerGroup] = {}
        self.attachments: set[tuple[GroupId, ServerId]] = set()

    def seed(self, group: PlayerGroup) -> None:
        self.by_id[group.id] = group

    @staticmethod
    def _copy(group: PlayerGroup) -> PlayerGroup:
        return replace(group, players=list(group.players))

    async def add(self, group: PlayerGroup) -> None:
        self.by_id[group.id] = self._copy(group)

    async def get_by_id(self, group_id: GroupId) -> PlayerGroup | None:
        group = self.by_id.get(group_id)
        return None if group is None else self._copy(group)

    async def get_by_community_kind_name(
        self, community_id: CommunityId, kind: GroupKind, name: GroupName
    ) -> PlayerGroup | None:
        for group in self.by_id.values():
            if (
                group.community_id == community_id
                and group.kind is kind
                and group.name == name
            ):
                return self._copy(group)
        return None

    async def list_for_community(self, community_id: CommunityId) -> list[PlayerGroup]:
        return [
            self._copy(g) for g in self.by_id.values() if g.community_id == community_id
        ]

    async def save(self, group: PlayerGroup) -> None:
        self.by_id[group.id] = self._copy(group)

    async def delete(self, group_id: GroupId) -> None:
        self.by_id.pop(group_id, None)
        self.attachments = {pair for pair in self.attachments if pair[0] != group_id}

    async def attach(self, group_id: GroupId, server_id: ServerId) -> None:
        self.attachments.add((group_id, server_id))

    async def detach(self, group_id: GroupId, server_id: ServerId) -> bool:
        if (group_id, server_id) not in self.attachments:
            return False
        self.attachments.discard((group_id, server_id))
        return True

    async def is_attached(self, group_id: GroupId, server_id: ServerId) -> bool:
        return (group_id, server_id) in self.attachments

    async def list_server_ids_for_group(self, group_id: GroupId) -> list[ServerId]:
        return sorted(
            (s for g, s in self.attachments if g == group_id),
            key=lambda s: str(s.value),
        )

    async def list_groups_for_server(self, server_id: ServerId) -> list[PlayerGroup]:
        return [
            self._copy(self.by_id[g])
            for g, s in sorted(self.attachments, key=lambda p: str(p[0].value))
            if s == server_id and g in self.by_id
        ]

    async def list_groups_for_server_kind(
        self, server_id: ServerId, kind: GroupKind
    ) -> list[PlayerGroup]:
        return [
            g for g in await self.list_groups_for_server(server_id) if g.kind is kind
        ]


class FakePluginRepository(PluginRepository):
    def __init__(self) -> None:
        self.by_id: dict[PluginId, ServerPlugin] = {}

    def seed(self, plugin: ServerPlugin) -> None:
        self.by_id[plugin.id] = plugin

    async def add(self, plugin: ServerPlugin) -> None:
        self.by_id[plugin.id] = plugin

    async def get_by_id(
        self, server_id: ServerId, plugin_id: PluginId
    ) -> ServerPlugin | None:
        plugin = self.by_id.get(plugin_id)
        if plugin is not None and plugin.server_id != server_id:
            return None
        return plugin

    async def list_for_server(self, server_id: ServerId) -> list[ServerPlugin]:
        return sorted(
            (p for p in self.by_id.values() if p.server_id == server_id),
            key=lambda p: (p.display_name, str(p.id.value)),
        )

    async def enabled_geyser_server_ids(
        self, server_ids: Iterable[ServerId]
    ) -> set[ServerId]:
        return {
            server_id
            for server_id in server_ids
            if has_enabled_geyser(await self.list_for_server(server_id))
        }

    async def delete(self, plugin_id: PluginId) -> None:
        self.by_id.pop(plugin_id, None)

    async def get_by_rel_path(
        self, server_id: ServerId, rel_path: str
    ) -> ServerPlugin | None:
        # Normalize the .disabled suffix so a clean path and its disabled variant
        # share the same per-server slot (issue #1316), mirroring the adapter:
        # prefer an exact-path match, else fall back to the suffix sibling.
        clean = rel_path.removesuffix(".disabled")
        candidates = [
            plugin
            for plugin in self.by_id.values()
            if plugin.server_id == server_id
            and plugin.rel_path.removesuffix(".disabled") == clean
        ]
        exact = next((p for p in candidates if p.rel_path == rel_path), None)
        if exact is not None:
            return exact
        return candidates[0] if candidates else None

    async def update(self, plugin: ServerPlugin) -> None:
        self.by_id[plugin.id] = plugin

    async def list_catalog_plugins(self, server_id: ServerId) -> list[ServerPlugin]:
        return sorted(
            (
                p
                for p in self.by_id.values()
                if p.server_id == server_id
                and p.source in CATALOG_SOURCES
                and p.source_project_id is not None
            ),
            key=lambda p: (p.display_name, str(p.id.value)),
        )

    async def get_by_source_project_id(
        self, server_id: ServerId, source_project_id: str
    ) -> ServerPlugin | None:
        for plugin in self.by_id.values():
            if (
                plugin.server_id == server_id
                and plugin.source_project_id == source_project_id
            ):
                return plugin
        return None

    async def all_sha256s(self) -> set[str]:
        return {p.sha256 for p in self.by_id.values() if p.sha256 is not None}

    async def find_catalog_provenance_by_sha512(
        self, checksum_sha512: str
    ) -> tuple[PluginSource, str] | None:
        for plugin in self.by_id.values():
            if (
                plugin.checksum_sha512 == checksum_sha512
                and plugin.source in CATALOG_SOURCES
                and plugin.source_project_id is not None
            ):
                return plugin.source, plugin.source_project_id
        return None

    async def find_sha256_by_sha512(self, checksum_sha512: str) -> str | None:
        for plugin in self.by_id.values():
            if plugin.checksum_sha512 == checksum_sha512 and plugin.sha256 is not None:
                return plugin.sha256
        return None


class FakeResourcePackRepository(ResourcePackRepository):
    def __init__(self) -> None:
        self.packs: dict[ResourcePackId, ResourcePack] = {}
        self.assignments: dict[ServerId, ResourcePackAssignment] = {}

    async def add(self, pack: ResourcePack) -> None:
        self.packs[pack.id] = pack

    async def get_by_id(self, pack_id: ResourcePackId) -> ResourcePack | None:
        return self.packs.get(pack_id)

    async def list_all(self) -> list[ResourcePack]:
        return sorted(
            self.packs.values(),
            key=lambda p: (p.display_name, str(p.id.value)),
        )

    async def delete(self, pack_id: ResourcePackId) -> None:
        self.packs.pop(pack_id, None)

    async def add_assignment(self, assignment: ResourcePackAssignment) -> None:
        self.assignments[assignment.server_id] = assignment

    async def get_assignment_by_server(
        self, server_id: ServerId
    ) -> ResourcePackAssignment | None:
        return self.assignments.get(server_id)

    async def delete_assignment(self, server_id: ServerId) -> None:
        self.assignments.pop(server_id, None)

    async def list_assignments_for_pack(
        self, pack_id: ResourcePackId
    ) -> list[ResourcePackAssignment]:
        return [a for a in self.assignments.values() if a.resource_pack_id == pack_id]


class FakeScheduleRepository(ScheduleRepository):
    """In-memory schedule store (issue #1835/#1837).

    Stored by id, returning detached copies so a use case mutating the loaded
    entity does not silently mutate the "persisted" one until ``update``.
    ``list_for_server`` mirrors the adapter's name ordering.
    """

    def __init__(self) -> None:
        self.by_id: dict[ScheduleId, Schedule] = {}

    def seed(self, schedule: Schedule) -> None:
        self.by_id[schedule.id] = schedule

    @staticmethod
    def _copy(schedule: Schedule) -> Schedule:
        return replace(schedule)

    async def add(self, schedule: Schedule) -> None:
        self.by_id[schedule.id] = self._copy(schedule)

    async def get_by_id(self, schedule_id: ScheduleId) -> Schedule | None:
        schedule = self.by_id.get(schedule_id)
        return None if schedule is None else self._copy(schedule)

    async def list_due(self, now: dt.datetime) -> list[Schedule]:
        due = [
            self._copy(s)
            for s in self.by_id.values()
            if s.enabled and s.next_run_at is not None and s.next_run_at <= now
        ]
        return sorted(due, key=lambda s: (s.next_run_at or now, s.id.value))

    async def list_warning_candidates(
        self, now: dt.datetime, until: dt.datetime
    ) -> list[Schedule]:
        # Mirror the adapter's look-ahead: enabled stop/restart rows whose
        # occurrence is still ahead but within the max warning offset.
        hits = [
            self._copy(s)
            for s in self.by_id.values()
            if s.enabled
            and s.action in (ScheduleAction.STOP, ScheduleAction.RESTART)
            and s.next_run_at is not None
            and now < s.next_run_at <= until
        ]
        return sorted(hits, key=lambda s: (s.next_run_at or until, s.id.value))

    async def list_for_server(self, server_id: ServerId) -> list[Schedule]:
        return sorted(
            (self._copy(s) for s in self.by_id.values() if s.server_id == server_id),
            key=lambda s: s.name,
        )

    async def update(self, schedule: Schedule) -> None:
        # Mirror the adapter's staged UPDATE: a missing id matches no row.
        if schedule.id in self.by_id:
            self.by_id[schedule.id] = self._copy(schedule)

    async def advance_run_state(
        self,
        schedule_id: ScheduleId,
        *,
        next_run_at: dt.datetime,
        last_run_at: dt.datetime | None,
    ) -> None:
        # Mirror the adapter's guarded bookkeeping UPDATE: only an enabled row
        # matches; a disabled/deleted schedule is silently left untouched.
        schedule = self.by_id.get(schedule_id)
        if schedule is None or not schedule.enabled:
            return
        self.by_id[schedule_id] = replace(
            schedule, next_run_at=next_run_at, last_run_at=last_run_at
        )

    async def delete(self, schedule_id: ScheduleId) -> None:
        self.by_id.pop(schedule_id, None)


class FakeScheduleRunRepository(ScheduleRunRepository):
    """In-memory schedule-run history store (issue #1835/#1837)."""

    def __init__(self) -> None:
        self.rows: list[ScheduleRun] = []

    def seed(self, run: ScheduleRun) -> None:
        self.rows.append(run)

    async def add(self, run: ScheduleRun) -> None:
        self.rows.append(run)

    async def list_for_schedule(self, schedule_id: ScheduleId) -> list[ScheduleRun]:
        return sorted(
            (r for r in self.rows if r.schedule_id == schedule_id),
            key=lambda r: (r.started_at, str(r.id.value)),
            reverse=True,
        )

    async def prune_for_schedule(self, schedule_id: ScheduleId, *, keep: int) -> None:
        kept = await self.list_for_schedule(schedule_id)
        stale = {r.id for r in kept[keep:]}
        self.rows = [r for r in self.rows if r.id not in stale]


class FakeServerNotifier(ServerNotifier):
    """Records published notifications for the runner tests."""

    def __init__(self) -> None:
        self.notifications: list[tuple[ServerId, str, str, str]] = []

    def notify(
        self, *, server_id: ServerId, kind: str, title: str, detail: str = ""
    ) -> None:
        self.notifications.append((server_id, kind, title, detail))


class FakeUnitOfWork(UnitOfWork):
    # Narrow the Port-declared attribute types to the concrete fakes so tests can
    # reach their inspection helpers without casts.
    servers: FakeServerRepository
    resource_grants: FakeResourceGrantSweeper
    backups: FakeBackupRepository
    groups: FakeGroupRepository
    game_sessions: FakeGameSessionRepository
    plugins: FakePluginRepository
    resource_packs: FakeResourcePackRepository
    schedules: FakeScheduleRepository
    schedule_runs: FakeScheduleRunRepository

    def __init__(
        self,
        servers: FakeServerRepository | None = None,
        resource_grants: FakeResourceGrantSweeper | None = None,
        backups: FakeBackupRepository | None = None,
        groups: FakeGroupRepository | None = None,
        game_sessions: FakeGameSessionRepository | None = None,
        plugins: FakePluginRepository | None = None,
        resource_packs: FakeResourcePackRepository | None = None,
        schedules: FakeScheduleRepository | None = None,
        schedule_runs: FakeScheduleRunRepository | None = None,
    ) -> None:
        self.servers = servers or FakeServerRepository()
        self.resource_grants = resource_grants or FakeResourceGrantSweeper()
        self.backups = backups or FakeBackupRepository()
        self.groups = groups or FakeGroupRepository()
        self.game_sessions = game_sessions or FakeGameSessionRepository()
        self.plugins = plugins or FakePluginRepository()
        self.resource_packs = resource_packs or FakeResourcePackRepository()
        self.schedules = schedules or FakeScheduleRepository()
        self.schedule_runs = schedule_runs or FakeScheduleRunRepository()
        self.commits = 0

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class FakeLifecycleLock(LifecycleLock):
    """Recording :class:`LifecycleLock` double for the use-case tests.

    Records ``(server_id, "acquire"|"release")`` events in order so a test can
    assert a gated use case (and StartServer's flip) takes the lock around its
    work. The actual cross-connection blocking is pinned against a real
    PostgreSQL advisory lock in the integration suite.
    """

    def __init__(self) -> None:
        self.events: list[tuple[ServerId, str]] = []

    @asynccontextmanager
    async def hold(self, server_id: ServerId) -> "AsyncIterator[None]":
        self.events.append((server_id, "acquire"))
        try:
            yield
        finally:
            self.events.append((server_id, "release"))


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
        held: dict[tuple[WorkerId, ServerId], int] | None = None,
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
        # Held-working-set generation map for the skip-hydrate decision (issue #763);
        # a (worker, server) pair absent here returns None (NOT held), so the default
        # is to hydrate.
        self._held = held or {}
        self.dispatched: list[tuple[str, WorkerId, ServerId]] = []
        # Command lines forwarded through command() — (server, line) — so a test
        # can assert the exact broadcast a scheduled warning sends (issue #1839).
        self.commands: list[tuple[ServerId, str]] = []
        self.incremented: list[WorkerId] = []
        self.decremented: list[WorkerId] = []
        # The (worker, server) placements reserved by place() and the reservations
        # released before commit, so a test can assert the #778 reservation lifecycle.
        self.reserved: list[tuple[WorkerId, ServerId]] = []
        self.released: list[tuple[WorkerId, ServerId]] = []
        # The ``force`` flag of the last stop dispatch, so a test can assert the
        # use case forwarded the caller's choice (issue #270).
        self.stop_force: bool | None = None

    async def place(
        self,
        *,
        server_id: ServerId,
        memory_limit_mb: int | None = None,
        committed_by_worker: dict[WorkerId, CommittedResources] | None = None,
    ) -> WorkerId | None:
        # Record the resource-aware placement inputs (#710) so a test can assert
        # the use case summed the committed accounting and forwarded the request's
        # memory; the placement decision itself stays the configured stub. Record
        # the reservation (#778) so a test can assert the chosen worker was reserved.
        self.place_memory_limit_mb = memory_limit_mb
        self.place_committed_by_worker = committed_by_worker or {}
        if self._place_to is not None:
            self.reserved.append((self._place_to, server_id))
        return self._place_to

    def is_worker_connected(self, *, worker_id: WorkerId) -> bool:
        return self._connected.get(worker_id, True)

    def held_generation(
        self, *, worker_id: WorkerId, server_id: ServerId
    ) -> int | None:
        return self._held.get((worker_id, server_id))

    def holds_fresh_working_set(
        self, *, worker_id: WorkerId, server_id: ServerId, store_generation: int
    ) -> bool:
        held = self._held.get((worker_id, server_id))
        return held is not None and held >= store_generation

    def increment_assignment(self, *, worker_id: WorkerId, server_id: ServerId) -> None:
        self.incremented.append(worker_id)

    def release_reservation(self, *, worker_id: WorkerId, server_id: ServerId) -> None:
        self.released.append((worker_id, server_id))

    def decrement_assignment(self, *, worker_id: WorkerId, server_id: ServerId) -> None:
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
        server_type: ServerType,
        jar_relpath: str,
        minecraft_version: str,
        memory_limit_bytes: int,
        cpu_millis: int,
    ) -> CommandOutcome:
        self.start_launch_server_type = server_type
        self.start_memory_limit_bytes = memory_limit_bytes
        self.start_cpu_millis = cpu_millis
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
        self.commands.append((server_id, line))
        return await self._record("command", worker_id, server_id)

    async def hydrate(
        self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
    ) -> CommandOutcome:
        return await self._record("hydrate", worker_id, server_id)

    async def snapshot(
        self,
        *,
        worker_id: WorkerId,
        community_id: CommunityId,
        server_id: ServerId,
        final: bool = False,
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
    delete-ordering (archive removed before the metadata row).
    ``create_from_current`` writes an archive under the caller-provided
    ``storage_ref``; ``missing`` makes the next create raise the no-working-set
    error.
    """

    def __init__(self, *, missing: bool = False, pack_fails: bool = False) -> None:
        self._missing = missing
        # When set, ``prune_to_final_snapshot`` raises so a test can assert the
        # DeleteServer pack is fail-closed (#777).
        self.pack_fails = pack_fails
        # Optional hook fired during the (potentially long) pack, so a test can
        # simulate a server start landing in the DeleteServer pack window (#777).
        self.on_prune: Callable[[], None] | None = None
        self.pruned: list[ServerId] = []
        self.archives: set[str] = set()
        # Bytes per stored archive, so open/store/size round-trip in tests.
        self.bytes_by_ref: dict[str, bytes] = {}
        self.created: list[ServerId] = []
        self.restored: list[tuple[ServerId, str]] = []
        # restore calls with their force flag, for the #743 gate tests.
        self.restore_calls: list[tuple[ServerId, str, bool]] = []
        # refs whose extracted working set is structurally corrupt (#743): without
        # force the restore raises BackupCorruptError; with force it publishes and
        # reports corruption. ``corrupt_count`` is the count carried on the error.
        self.corrupt_refs: set[str] = set()
        self.corrupt_count = 1
        # The sweep (#744) snapshot fsck: corrupt-region count of each server's
        # published ``current``; a server absent here has no published snapshot, so
        # ``check_current_health`` returns None (nothing to fsck).
        self.current_corrupt: dict[ServerId, int] = {}
        self.deleted: list[tuple[ServerId, str]] = []
        self.stored: list[ServerId] = []
        # storage_refs that ``size`` was called for, so a test can assert the
        # lazy size backfill (#661) only calls per NULL row and not again once
        # the row's size is persisted.
        self.size_calls: list[str] = []
        # When set, ``size`` raises this instead of returning a size, modelling a
        # non-404 store failure (object-store ClientError, connection error, fs
        # OSError) so a test can assert the backfill stays best-effort (#661).
        self.size_error: Exception | None = None
        self._counter = 0

    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        if self._missing:
            raise BackupNotFoundError(str(server_id.value))
        self.archives.add(storage_ref)
        self.bytes_by_ref[storage_ref] = b"archive-bytes"
        self.created.append(server_id)

    async def list_archive_refs(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> list[str]:
        return sorted(self.archives)

    async def restore(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        storage_ref: str,
        force: bool = False,
    ) -> int:
        if storage_ref not in self.archives:
            raise BackupNotFoundError(storage_ref)
        self.restore_calls.append((server_id, storage_ref, force))
        if storage_ref in self.corrupt_refs and not force:
            raise BackupCorruptError(storage_ref, corrupt_count=self.corrupt_count)
        self.restored.append((server_id, storage_ref))
        return self.corrupt_count if storage_ref in self.corrupt_refs else 0

    async def check_backup_health(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> int:
        if storage_ref not in self.archives:
            raise BackupNotFoundError(storage_ref)
        return self.corrupt_count if storage_ref in self.corrupt_refs else 0

    async def check_current_health(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> int | None:
        return self.current_corrupt.get(server_id)

    async def delete(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        self.archives.discard(storage_ref)
        self.bytes_by_ref.pop(storage_ref, None)
        self.deleted.append((server_id, storage_ref))

    async def prune_to_final_snapshot(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> None:
        # The DeleteServer reclaim path (#777). ``pack_fails`` makes it raise so a
        # test can assert the delete aborts with the working set intact.
        if self.pack_fails:
            raise RuntimeError("pack failed")
        if self.on_prune is not None:
            self.on_prune()
        self.pruned.append(server_id)

    async def open(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> AsyncIterator[bytes]:
        if storage_ref not in self.archives:
            raise BackupNotFoundError(storage_ref)
        yield self.bytes_by_ref[storage_ref]

    async def store(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        stream: AsyncIterator[bytes],
        storage_ref: str,
    ) -> None:
        data = b"".join([chunk async for chunk in stream])
        self.archives.add(storage_ref)
        self.bytes_by_ref[storage_ref] = data
        self.stored.append(server_id)

    async def size(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> int:
        self.size_calls.append(storage_ref)
        if self.size_error is not None:
            raise self.size_error
        if storage_ref not in self.archives:
            raise BackupNotFoundError(storage_ref)
        return len(self.bytes_by_ref[storage_ref])


class FakeCatalogProvider(CatalogProvider):
    """In-memory :class:`CatalogProvider` double for catalog use-case tests.

    Stores projects, versions, and downloadable file bytes. Search returns all
    seeded projects (no actual text matching). ``unavailable`` makes every call
    raise :class:`CatalogUnavailableError`.
    """

    def __init__(self, *, unavailable: bool = False) -> None:
        self.projects: dict[str, CatalogProject] = {}
        self.versions: dict[str, list[CatalogVersion]] = {}
        self.file_bytes: dict[str, bytes] = {}
        # Records each download_file URL so a test can assert the download cache
        # skipped an HTTP fetch for an already-cached Modrinth version (#1306).
        self.downloads: list[str] = []
        self._unavailable = unavailable

    def seed_project(
        self,
        project: CatalogProject,
        versions: list[CatalogVersion] | None = None,
    ) -> None:
        self.projects[project.project_id] = project
        self.projects[project.slug] = project
        if versions:
            self.versions.setdefault(project.project_id, []).extend(versions)
            self.versions.setdefault(project.slug, []).extend(versions)

    def seed_file(self, url: str, content: bytes) -> None:
        self.file_bytes[url] = content

    async def search(
        self,
        *,
        query: str,
        loader: str,
        game_versions: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> CatalogSearchResponse:
        from mc_server_dashboard_api.servers.domain.errors import (
            CatalogUnavailableError,
        )

        if self._unavailable:
            raise CatalogUnavailableError("fake unavailable")
        # Deduplicate by project_id (seeded twice: by id and slug).
        seen: set[str] = set()
        hits: list[CatalogSearchResult] = []
        for project in self.projects.values():
            if project.project_id in seen:
                continue
            seen.add(project.project_id)
            hits.append(
                CatalogSearchResult(
                    project_id=project.project_id,
                    slug=project.slug,
                    title=project.title,
                    description=project.description,
                    author=project.author or "",
                    icon_url=project.icon_url,
                    downloads=project.downloads,
                    categories=project.categories,
                    latest_game_versions=project.game_versions,
                )
            )
        page = hits[offset : offset + limit]
        return CatalogSearchResponse(
            hits=page, total_hits=len(hits), offset=offset, limit=limit
        )

    async def get_project(self, project_id_or_slug: str) -> CatalogProject:
        from mc_server_dashboard_api.servers.domain.errors import (
            CatalogProjectNotFoundError,
            CatalogUnavailableError,
        )

        if self._unavailable:
            raise CatalogUnavailableError("fake unavailable")
        project = self.projects.get(project_id_or_slug)
        if project is None:
            raise CatalogProjectNotFoundError(project_id_or_slug)
        return project

    async def list_versions(
        self,
        project_id_or_slug: str,
        *,
        loader: str | None = None,
        game_versions: list[str] | None = None,
    ) -> list[CatalogVersion]:
        from mc_server_dashboard_api.servers.domain.errors import (
            CatalogUnavailableError,
        )

        if self._unavailable:
            raise CatalogUnavailableError("fake unavailable")
        return self.versions.get(project_id_or_slug, [])

    async def download_file(self, url: str) -> bytes:
        from mc_server_dashboard_api.servers.domain.errors import (
            CatalogProjectNotFoundError,
            CatalogUnavailableError,
        )

        if self._unavailable:
            raise CatalogUnavailableError("fake unavailable")
        self.downloads.append(url)
        content = self.file_bytes.get(url)
        if content is None:
            raise CatalogProjectNotFoundError(url)
        return content


class FakeResourcePackStore(ResourcePackStore):
    """In-memory resource pack blob store for use-case tests (issue #1176)."""

    def __init__(self) -> None:
        self.blobs: dict[ResourcePackId, bytes] = {}

    async def put(
        self,
        pack_id: ResourcePackId,
        filename: str,
        stream: AsyncIterator[bytes],
    ) -> None:
        data = b"".join([chunk async for chunk in stream])
        self.blobs[pack_id] = data

    def open(self, pack_id: ResourcePackId, filename: str) -> AsyncIterator[bytes]:
        data = self.blobs[pack_id]

        async def _gen() -> AsyncIterator[bytes]:
            yield data

        return _gen()

    async def delete(self, pack_id: ResourcePackId) -> None:
        self.blobs.pop(pack_id, None)

    async def size(self, pack_id: ResourcePackId, filename: str) -> int:
        return len(self.blobs[pack_id])


class FakePluginCacheStore(PluginCacheStore):
    """In-memory content-addressed plugin cache for use-case tests (issue #1306).

    Keyed by SHA-256 content address. ``puts`` records each ``put`` call (even the
    deduped ones) and ``stored`` holds the keys actually persisted, so a test can
    assert dedup (a second put of identical bytes does not grow ``stored``) and the
    download cache (a cached ``has`` short-circuits the HTTP download).
    """

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.puts: list[str] = []

    async def has(self, sha256: str) -> bool:
        return sha256 in self.blobs

    async def put(self, sha256: str, stream: AsyncIterator[bytes]) -> None:
        self.puts.append(sha256)
        data = b"".join([chunk async for chunk in stream])
        # Dedup-on-ingest: identical content addresses the same key.
        self.blobs.setdefault(sha256, data)

    def open(self, sha256: str) -> AsyncIterator[bytes]:
        data = self.blobs[sha256]

        async def _gen() -> AsyncIterator[bytes]:
            yield data

        return _gen()

    async def list_entries(self) -> list[CacheEntry]:
        return [
            CacheEntry(
                sha256=sha,
                size_bytes=len(data),
                modified_at=dt.datetime.now(dt.UTC),
            )
            for sha, data in self.blobs.items()
        ]

    async def delete(self, sha256: str) -> None:
        self.blobs.pop(sha256, None)


class FakeBedrockTunnelSync(BedrockTunnelSync):
    """Recording :class:`BedrockTunnelSync` double (issue #1602).

    Records ``(server_id, worker_id, bedrock_port, running)`` tuples so a test
    can assert the lifecycle invoked the tunnel sync on the INVALID_STATE and
    confirmed-stop convergence paths.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[ServerId, WorkerId, int | None, bool]] = []

    async def sync_observed(
        self,
        *,
        server_id: ServerId,
        worker_id: WorkerId,
        bedrock_port: int | None,
        running: bool,
    ) -> None:
        self.calls.append((server_id, worker_id, bedrock_port, running))
