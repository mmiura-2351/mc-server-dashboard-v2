"""Fleet-backed adapter for the servers :class:`ControlPlane` seam.

Binds the lifecycle layer's control-plane Port to the real fleet machinery: the
:class:`WorkerRegistry` (placement + load tracking) and the fleet
:class:`fleet ControlPlane <...fleet.domain.control_plane.ControlPlane>` (command
dispatch over the gRPC stream). This is an adapter-layer composition across
bounded contexts (mirroring the servers UnitOfWork reusing the community
resource-grant adapter); the servers *domain* and *application* never import the
fleet context (import-linter contract).

The driver-spelling map lives here, at the seam: the servers
:class:`ExecutionBackend` uses the underscore spelling DATABASE.md's CHECK enum
mandates (``host_process``); the fleet :class:`DriverKind` uses the hyphen
spelling (``host-process``). The two enums are deliberately not shared, so the
mapping is an adapter concern (servers/domain/value_objects.py).
"""

from __future__ import annotations

import uuid

from mc_server_dashboard_api.fleet.domain.control_plane import (
    CommandResult,
    CommandResultCode,
    CommandTimedOutError,
    EditFileCommand,
    HydrateCommand,
    LaunchMode,
    ListFilesCommand,
    ReadFileCommand,
    RestartServerCommand,
    ServerCommandCommand,
    SnapshotCommand,
    StartServerCommand,
    StopServerCommand,
    WorkerNotConnectedError,
)
from mc_server_dashboard_api.fleet.domain.control_plane import (
    ControlPlane as FleetControlPlane,
)
from mc_server_dashboard_api.fleet.domain.control_plane import (
    FileAccessReason as FleetFileAccessReason,
)
from mc_server_dashboard_api.fleet.domain.entities import WorkerStatus
from mc_server_dashboard_api.fleet.domain.placement import place
from mc_server_dashboard_api.fleet.domain.registry import WorkerRegistry
from mc_server_dashboard_api.fleet.domain.value_objects import DriverKind
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId as FleetWorkerId
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
    ControlPlane,
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    FileAccessReason as OutcomeFileAccessReason,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    FileEntry as OutcomeFileEntry,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    FileListing as OutcomeFileListing,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ExecutionBackend,
    ServerId,
    ServerType,
    WorkerId,
)

# Map the servers backend enum (underscore spelling) to the fleet driver enum
# (hyphen spelling). The two are intentionally distinct domain types.
_DRIVER_BY_BACKEND: dict[ExecutionBackend, DriverKind] = {
    ExecutionBackend.HOST_PROCESS: DriverKind.HOST_PROCESS,
    ExecutionBackend.CONTAINER: DriverKind.CONTAINER,
}

# Map the fleet result code to the servers outcome status (same names, distinct
# enums on either side of the seam).
_STATUS_BY_CODE: dict[CommandResultCode, CommandStatus] = {
    CommandResultCode.OK: CommandStatus.OK,
    CommandResultCode.SERVER_NOT_FOUND: CommandStatus.SERVER_NOT_FOUND,
    CommandResultCode.INVALID_STATE: CommandStatus.INVALID_STATE,
    CommandResultCode.DRIVER_UNAVAILABLE: CommandStatus.DRIVER_UNAVAILABLE,
    CommandResultCode.FILE_ACCESS_DENIED: CommandStatus.FILE_ACCESS_DENIED,
    CommandResultCode.TRANSFER_FAILED: CommandStatus.TRANSFER_FAILED,
    CommandResultCode.INTERNAL: CommandStatus.INTERNAL,
    CommandResultCode.PORT_CONFLICT: CommandStatus.PORT_CONFLICT,
    CommandResultCode.IMAGE_MISSING: CommandStatus.IMAGE_MISSING,
}

# Map the fleet file-access reason to the servers outcome reason (issue #548;
# same names, distinct enums on either side of the seam).
_REASON_BY_FLEET_REASON: dict[FleetFileAccessReason, OutcomeFileAccessReason] = {
    FleetFileAccessReason.UNSPECIFIED: OutcomeFileAccessReason.UNSPECIFIED,
    FleetFileAccessReason.IS_A_DIRECTORY: OutcomeFileAccessReason.IS_A_DIRECTORY,
    FleetFileAccessReason.NOT_A_DIRECTORY: OutcomeFileAccessReason.NOT_A_DIRECTORY,
    FleetFileAccessReason.SYMLINK_REFUSED: OutcomeFileAccessReason.SYMLINK_REFUSED,
    FleetFileAccessReason.PAYLOAD_TOO_LARGE: OutcomeFileAccessReason.PAYLOAD_TOO_LARGE,
}


def _launch_mode_for(server_type: ServerType) -> LaunchMode:
    """Forge launches via the supervised installer + args file; all else via JAR.

    The launch mode is carried explicitly on StartServer (issue #307); the Worker
    never infers it from the working-set contents (CONTROL_PLANE.md Section 5).
    """

    if server_type is ServerType.FORGE:
        return LaunchMode.FORGE_ARGSFILE
    return LaunchMode.JAR


def _to_outcome(result: CommandResult) -> CommandOutcome:
    listing = None
    if result.file_listing is not None:
        listing = OutcomeFileListing(
            entries=tuple(
                OutcomeFileEntry(name=e.name, is_dir=e.is_dir, size=e.size)
                for e in result.file_listing.entries
            ),
            truncated=result.file_listing.truncated,
        )
    return CommandOutcome(
        status=_STATUS_BY_CODE[result.code],
        message=result.message,
        output=result.output,
        file_content=result.file_content,
        listing=listing,
        file_access_reason=_REASON_BY_FLEET_REASON[result.file_access_reason],
    )


def _fleet_worker(worker_id: WorkerId) -> FleetWorkerId:
    return FleetWorkerId(str(worker_id.value))


def _servers_worker(worker_id: FleetWorkerId) -> WorkerId:
    # The fleet worker id is the registry key string; servers persist it as a
    # plain UUID (PM ruling on #93: no worker table, assigned_worker_id is a UUID).
    # At M1 a Worker is expected to register with a UUID-format id so the two
    # sides round-trip; the seam is the single place that bridges str <-> UUID.
    return WorkerId(uuid.UUID(worker_id.value))


class FleetControlPlaneAdapter(ControlPlane):
    """Bind the servers control-plane seam to the registry + fleet control plane."""

    def __init__(
        self,
        *,
        registry: WorkerRegistry,
        control_plane: FleetControlPlane,
        data_plane_base_url: str | None = None,
        worker_credential: str | None = None,
    ) -> None:
        self._registry = registry
        self._control_plane = control_plane
        # The externally reachable data-plane base URL and the shared Worker
        # credential (the transfer token) are only needed to dispatch a
        # hydrate/snapshot; lifecycle-only callers may leave them unset.
        self._data_plane_base_url = data_plane_base_url
        self._worker_credential = worker_credential

    async def place(self, *, backend: ExecutionBackend) -> WorkerId | None:
        chosen = place(
            self._registry.candidates_for_placement(),
            required_driver=_DRIVER_BY_BACKEND[backend],
        )
        if isinstance(chosen, FleetWorkerId):
            return _servers_worker(chosen)
        return None

    def is_worker_connected(self, *, worker_id: WorkerId) -> bool:
        # The registry resolves liveness at read time from the heartbeat clock;
        # ONLINE means the Worker has a live, recently-beating session. A DRAINING
        # Worker stays connected (it just declines new placement), so it counts as
        # connected for snapshots of servers already on it.
        snapshot = self._registry.get(_fleet_worker(worker_id))
        return snapshot is not None and snapshot.status in (
            WorkerStatus.ONLINE,
            WorkerStatus.DRAINING,
        )

    def holds_working_set(self, *, worker_id: WorkerId, server_id: ServerId) -> bool:
        # Read the held-working-set inventory the Worker advertised on Register
        # (issue #696). The registry keys it by the fleet worker-id string and the
        # server-id string (the wire spelling); the seam bridges both.
        return self._registry.holds_working_set(
            _fleet_worker(worker_id), str(server_id.value)
        )

    def increment_assignment(self, *, worker_id: WorkerId) -> None:
        self._registry.increment_assignment(_fleet_worker(worker_id))

    def decrement_assignment(self, *, worker_id: WorkerId) -> None:
        self._registry.decrement_assignment(_fleet_worker(worker_id))

    async def start(
        self,
        *,
        worker_id: WorkerId,
        server_id: ServerId,
        backend: ExecutionBackend,
        server_type: ServerType,
        jar_relpath: str,
        minecraft_version: str,
        memory_limit_bytes: int,
        cpu_millis: int,
    ) -> CommandOutcome:
        return await self._dispatch(
            worker_id,
            server_id,
            StartServerCommand(
                driver=_DRIVER_BY_BACKEND[backend],
                jar_relpath=jar_relpath,
                minecraft_version=minecraft_version,
                launch_mode=_launch_mode_for(server_type),
                memory_limit_bytes=memory_limit_bytes,
                cpu_millis=cpu_millis,
            ),
        )

    async def stop(
        self, *, worker_id: WorkerId, server_id: ServerId, force: bool = False
    ) -> CommandOutcome:
        return await self._dispatch(
            worker_id, server_id, StopServerCommand(force=force)
        )

    async def restart(
        self, *, worker_id: WorkerId, server_id: ServerId
    ) -> CommandOutcome:
        return await self._dispatch(worker_id, server_id, RestartServerCommand())

    async def command(
        self, *, worker_id: WorkerId, server_id: ServerId, line: str
    ) -> CommandOutcome:
        return await self._dispatch(
            worker_id, server_id, ServerCommandCommand(line=line)
        )

    async def hydrate(
        self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
    ) -> CommandOutcome:
        url = self._working_set_url(community_id, server_id)
        return await self._dispatch(
            worker_id,
            server_id,
            HydrateCommand(transfer_url=url, transfer_token=self._token()),
        )

    async def snapshot(
        self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
    ) -> CommandOutcome:
        url = self._snapshot_url(community_id, server_id)
        return await self._dispatch(
            worker_id,
            server_id,
            SnapshotCommand(transfer_url=url, transfer_token=self._token()),
        )

    async def read_file(
        self, *, worker_id: WorkerId, server_id: ServerId, rel_path: str
    ) -> CommandOutcome:
        return await self._dispatch(
            worker_id, server_id, ReadFileCommand(path=rel_path)
        )

    async def edit_file(
        self,
        *,
        worker_id: WorkerId,
        server_id: ServerId,
        rel_path: str,
        content: bytes,
    ) -> CommandOutcome:
        return await self._dispatch(
            worker_id, server_id, EditFileCommand(path=rel_path, content=content)
        )

    async def list_files(
        self, *, worker_id: WorkerId, server_id: ServerId, rel_path: str
    ) -> CommandOutcome:
        return await self._dispatch(
            worker_id, server_id, ListFilesCommand(path=rel_path)
        )

    def _base(self) -> str:
        if not self._data_plane_base_url:
            raise WorkerUnavailableError(
                "data-plane transfer requested but server.public_base_url is unset"
            )
        return self._data_plane_base_url.rstrip("/")

    def _token(self) -> str:
        if not self._worker_credential:
            raise WorkerUnavailableError(
                "data-plane transfer requested but the Worker credential is unset"
            )
        return self._worker_credential

    def _working_set_url(self, community_id: CommunityId, server_id: ServerId) -> str:
        return (
            f"{self._base()}/api/data-plane/communities/{community_id.value}"
            f"/servers/{server_id.value}/working-set"
        )

    def _snapshot_url(self, community_id: CommunityId, server_id: ServerId) -> str:
        return (
            f"{self._base()}/api/data-plane/communities/{community_id.value}"
            f"/servers/{server_id.value}/snapshot"
        )

    async def _dispatch(
        self,
        worker_id: WorkerId,
        server_id: ServerId,
        command: StartServerCommand
        | StopServerCommand
        | RestartServerCommand
        | ServerCommandCommand
        | HydrateCommand
        | SnapshotCommand
        | ReadFileCommand
        | EditFileCommand
        | ListFilesCommand,
    ) -> CommandOutcome:
        try:
            result = await self._control_plane.dispatch(
                worker_id=_fleet_worker(worker_id),
                server_id=str(server_id.value),
                command=command,
            )
        except (WorkerNotConnectedError, CommandTimedOutError) as exc:
            raise WorkerUnavailableError(str(worker_id.value)) from exc
        return _to_outcome(result)
