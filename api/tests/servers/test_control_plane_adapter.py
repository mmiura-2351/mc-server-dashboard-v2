"""Tests for the servers control-plane adapter's data-plane URL building (#106).

The adapter must address the data-plane endpoint for a ``(community, server)``
scope and hand the Worker the URL + the shared credential as the transfer token.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.domain.control_plane import (
    Command,
    CommandResult,
    CommandResultCode,
    EditFileCommand,
    HydrateCommand,
    LaunchMode,
    ListFilesCommand,
    ReadFileCommand,
    SnapshotCommand,
    StartServerCommand,
)
from mc_server_dashboard_api.fleet.domain.control_plane import (
    ControlPlane as FleetControlPlane,
)
from mc_server_dashboard_api.fleet.domain.control_plane import (
    FileAccessReason as FleetFileAccessReason,
)
from mc_server_dashboard_api.fleet.domain.control_plane import (
    FileEntry as FleetFileEntry,
)
from mc_server_dashboard_api.fleet.domain.control_plane import (
    FileListing as FleetFileListing,
)
from mc_server_dashboard_api.fleet.domain.value_objects import HostResources
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId as FleetWorkerId
from mc_server_dashboard_api.servers.adapters.control_plane import (
    FleetControlPlaneAdapter,
)
from mc_server_dashboard_api.servers.domain.committed_resources import (
    CommittedResources,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandStatus,
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    FileAccessReason as OutcomeFileAccessReason,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ExecutionBackend,
    ServerId,
    ServerType,
    WorkerId,
)
from tests.fleet.fakes import FakeClock, make_worker

_T0 = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_TIMEOUT = dt.timedelta(seconds=30)


class _CapturingFleetControlPlane(FleetControlPlane):
    def __init__(self, *, result: CommandResult | None = None) -> None:
        self.last: Command | None = None
        self._result = result or CommandResult(code=CommandResultCode.OK)

    async def dispatch(
        self, *, worker_id: FleetWorkerId, server_id: str, command: Command
    ) -> CommandResult:
        self.last = command
        return self._result


def _adapter(fleet: FleetControlPlane) -> FleetControlPlaneAdapter:
    return FleetControlPlaneAdapter(
        registry=None,  # type: ignore[arg-type]  # unused by hydrate/snapshot
        control_plane=fleet,
        data_plane_base_url="https://api.example/",
        worker_credential="shhh",
    )


async def test_hydrate_builds_working_set_url_and_token() -> None:
    fleet = _CapturingFleetControlPlane()
    adapter = _adapter(fleet)
    community = uuid.uuid4()
    server = uuid.uuid4()

    outcome = await adapter.hydrate(
        worker_id=WorkerId(uuid.uuid4()),
        community_id=CommunityId(community),
        server_id=ServerId(server),
    )

    assert outcome.success
    assert isinstance(fleet.last, HydrateCommand)
    assert fleet.last.transfer_url == (
        f"https://api.example/api/data-plane/communities/{community}"
        f"/servers/{server}/working-set"
    )
    assert fleet.last.transfer_token == "shhh"


async def test_snapshot_builds_snapshot_url() -> None:
    fleet = _CapturingFleetControlPlane()
    adapter = _adapter(fleet)
    community = uuid.uuid4()
    server = uuid.uuid4()

    await adapter.snapshot(
        worker_id=WorkerId(uuid.uuid4()),
        community_id=CommunityId(community),
        server_id=ServerId(server),
    )

    assert isinstance(fleet.last, SnapshotCommand)
    assert fleet.last.transfer_url == (
        f"https://api.example/api/data-plane/communities/{community}"
        f"/servers/{server}/snapshot"
    )


async def test_read_file_dispatches_and_carries_bytes() -> None:
    fleet = _CapturingFleetControlPlane(
        result=CommandResult(code=CommandResultCode.OK, file_content=b"\x00bytes")
    )
    adapter = _adapter(fleet)

    outcome = await adapter.read_file(
        worker_id=WorkerId(uuid.uuid4()),
        server_id=ServerId(uuid.uuid4()),
        rel_path="server.properties",
    )
    assert isinstance(fleet.last, ReadFileCommand)
    assert fleet.last.path == "server.properties"
    assert outcome.success
    assert outcome.file_content == b"\x00bytes"


async def test_edit_file_dispatches_content() -> None:
    fleet = _CapturingFleetControlPlane()
    adapter = _adapter(fleet)

    await adapter.edit_file(
        worker_id=WorkerId(uuid.uuid4()),
        server_id=ServerId(uuid.uuid4()),
        rel_path="ops.json",
        content=b"[]",
    )
    assert isinstance(fleet.last, EditFileCommand)
    assert fleet.last.path == "ops.json"
    assert fleet.last.content == b"[]"


async def test_list_files_dispatches_and_carries_listing() -> None:
    fleet = _CapturingFleetControlPlane(
        result=CommandResult(
            code=CommandResultCode.OK,
            file_listing=FleetFileListing(
                entries=(
                    FleetFileEntry(name="config.yml", is_dir=False, size=12),
                    FleetFileEntry(name="data", is_dir=True, size=0),
                ),
                truncated=True,
            ),
        )
    )
    adapter = _adapter(fleet)

    outcome = await adapter.list_files(
        worker_id=WorkerId(uuid.uuid4()),
        server_id=ServerId(uuid.uuid4()),
        rel_path="plugins",
    )
    assert isinstance(fleet.last, ListFilesCommand)
    assert fleet.last.path == "plugins"
    assert outcome.success
    assert outcome.listing is not None
    assert outcome.listing.truncated is True
    assert [(e.name, e.is_dir, e.size) for e in outcome.listing.entries] == [
        ("config.yml", False, 12),
        ("data", True, 0),
    ]


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (CommandResultCode.PORT_CONFLICT, CommandStatus.PORT_CONFLICT),
        (CommandResultCode.IMAGE_MISSING, CommandStatus.IMAGE_MISSING),
    ],
)
async def test_sanitized_start_failure_maps_to_status(
    code: CommandResultCode, status: CommandStatus
) -> None:
    # The Worker's sanitized start-failure codes (issue #225) carry through the
    # fleet result code to the servers-side outcome status.
    fleet = _CapturingFleetControlPlane(result=CommandResult(code=code, message="x"))
    adapter = _adapter(fleet)

    outcome = await adapter.start(
        worker_id=WorkerId(uuid.uuid4()),
        server_id=ServerId(uuid.uuid4()),
        backend=ExecutionBackend.HOST_PROCESS,
        server_type=ServerType.VANILLA,
        jar_relpath="server.jar",
        minecraft_version="1.21",
        memory_limit_bytes=0,
        cpu_millis=0,
    )
    assert outcome.status is status


@pytest.mark.parametrize(
    ("server_type", "launch_mode"),
    [
        (ServerType.FORGE, LaunchMode.FORGE_ARGSFILE),
        (ServerType.VANILLA, LaunchMode.JAR),
        (ServerType.PAPER, LaunchMode.JAR),
        (ServerType.FABRIC, LaunchMode.JAR),
    ],
)
async def test_start_maps_server_type_to_launch_mode(
    server_type: ServerType, launch_mode: LaunchMode
) -> None:
    # Forge launches via the supervised installer + args file; every other type
    # via the historical JAR launch (issue #307).
    fleet = _CapturingFleetControlPlane()
    adapter = _adapter(fleet)

    await adapter.start(
        worker_id=WorkerId(uuid.uuid4()),
        server_id=ServerId(uuid.uuid4()),
        backend=ExecutionBackend.HOST_PROCESS,
        server_type=server_type,
        jar_relpath="server.jar",
        minecraft_version="1.21",
        memory_limit_bytes=0,
        cpu_millis=0,
    )

    assert isinstance(fleet.last, StartServerCommand)
    assert fleet.last.launch_mode is launch_mode


async def test_start_threads_memory_limit_bytes_to_the_command() -> None:
    # The per-server memory limit (#706) is carried straight through onto the fleet
    # StartServerCommand in bytes; the adapter does not reinterpret it.
    fleet = _CapturingFleetControlPlane()
    adapter = _adapter(fleet)

    await adapter.start(
        worker_id=WorkerId(uuid.uuid4()),
        server_id=ServerId(uuid.uuid4()),
        backend=ExecutionBackend.HOST_PROCESS,
        server_type=ServerType.VANILLA,
        jar_relpath="server.jar",
        minecraft_version="1.21",
        memory_limit_bytes=4096 * 1024 * 1024,
        cpu_millis=0,
    )

    assert isinstance(fleet.last, StartServerCommand)
    assert fleet.last.memory_limit_bytes == 4096 * 1024 * 1024


async def test_start_threads_cpu_millis_to_the_command() -> None:
    # The per-server CPU allocation (#723) is carried straight through onto the fleet
    # StartServerCommand in millicores; the adapter does not reinterpret it.
    fleet = _CapturingFleetControlPlane()
    adapter = _adapter(fleet)

    await adapter.start(
        worker_id=WorkerId(uuid.uuid4()),
        server_id=ServerId(uuid.uuid4()),
        backend=ExecutionBackend.HOST_PROCESS,
        server_type=ServerType.VANILLA,
        jar_relpath="server.jar",
        minecraft_version="1.21",
        memory_limit_bytes=0,
        cpu_millis=2000,
    )

    assert isinstance(fleet.last, StartServerCommand)
    assert fleet.last.cpu_millis == 2000


async def test_file_access_denied_maps_to_status() -> None:
    fleet = _CapturingFleetControlPlane(
        result=CommandResult(code=CommandResultCode.FILE_ACCESS_DENIED, message="nope")
    )
    adapter = _adapter(fleet)

    outcome = await adapter.read_file(
        worker_id=WorkerId(uuid.uuid4()),
        server_id=ServerId(uuid.uuid4()),
        rel_path="../escape",
    )
    assert outcome.status is CommandStatus.FILE_ACCESS_DENIED
    assert outcome.message == "nope"
    # The default (unrefined) reason rides through as UNSPECIFIED.
    assert outcome.file_access_reason is OutcomeFileAccessReason.UNSPECIFIED


@pytest.mark.parametrize(
    ("fleet_reason", "outcome_reason"),
    [
        (
            FleetFileAccessReason.IS_A_DIRECTORY,
            OutcomeFileAccessReason.IS_A_DIRECTORY,
        ),
        (
            FleetFileAccessReason.NOT_A_DIRECTORY,
            OutcomeFileAccessReason.NOT_A_DIRECTORY,
        ),
        (
            FleetFileAccessReason.SYMLINK_REFUSED,
            OutcomeFileAccessReason.SYMLINK_REFUSED,
        ),
        (
            FleetFileAccessReason.PAYLOAD_TOO_LARGE,
            OutcomeFileAccessReason.PAYLOAD_TOO_LARGE,
        ),
    ],
)
async def test_file_access_reason_maps_through_seam(
    fleet_reason: FleetFileAccessReason, outcome_reason: OutcomeFileAccessReason
) -> None:
    fleet = _CapturingFleetControlPlane(
        result=CommandResult(
            code=CommandResultCode.FILE_ACCESS_DENIED,
            message="nope",
            file_access_reason=fleet_reason,
        )
    )
    adapter = _adapter(fleet)

    outcome = await adapter.read_file(
        worker_id=WorkerId(uuid.uuid4()),
        server_id=ServerId(uuid.uuid4()),
        rel_path="config",
    )
    assert outcome.status is CommandStatus.FILE_ACCESS_DENIED
    assert outcome.file_access_reason is outcome_reason


async def test_hydrate_without_base_url_is_worker_unavailable() -> None:
    fleet = _CapturingFleetControlPlane()
    adapter = FleetControlPlaneAdapter(
        registry=None,  # type: ignore[arg-type]
        control_plane=fleet,
        data_plane_base_url=None,
        worker_credential="shhh",
    )
    with pytest.raises(WorkerUnavailableError):
        await adapter.hydrate(
            worker_id=WorkerId(uuid.uuid4()),
            community_id=CommunityId(uuid.uuid4()),
            server_id=ServerId(uuid.uuid4()),
        )


# --- is_worker_connected via the registry per-id lookup (#322) --------------


def _registry_adapter(
    registry: InMemoryWorkerRegistry,
) -> FleetControlPlaneAdapter:
    return FleetControlPlaneAdapter(
        registry=registry,
        control_plane=_CapturingFleetControlPlane(),
    )


def test_is_worker_connected_true_for_online_worker() -> None:
    worker_uuid = uuid.uuid4()
    clock = FakeClock(_T0)
    registry = InMemoryWorkerRegistry(clock=clock, heartbeat_timeout=_TIMEOUT)
    registry.register(make_worker(worker_id=str(worker_uuid), at=_T0))

    adapter = _registry_adapter(registry)

    assert adapter.is_worker_connected(worker_id=WorkerId(worker_uuid)) is True


def test_is_worker_connected_true_for_draining_worker() -> None:
    worker_uuid = uuid.uuid4()
    clock = FakeClock(_T0)
    registry = InMemoryWorkerRegistry(clock=clock, heartbeat_timeout=_TIMEOUT)
    registry.register(make_worker(worker_id=str(worker_uuid), at=_T0))
    registry.set_draining(FleetWorkerId(str(worker_uuid)), True)

    adapter = _registry_adapter(registry)

    assert adapter.is_worker_connected(worker_id=WorkerId(worker_uuid)) is True


def test_is_worker_connected_false_for_unknown_worker() -> None:
    clock = FakeClock(_T0)
    registry = InMemoryWorkerRegistry(clock=clock, heartbeat_timeout=_TIMEOUT)

    adapter = _registry_adapter(registry)

    assert adapter.is_worker_connected(worker_id=WorkerId(uuid.uuid4())) is False


def test_is_worker_connected_false_for_offline_worker() -> None:
    worker_uuid = uuid.uuid4()
    clock = FakeClock(_T0)
    registry = InMemoryWorkerRegistry(clock=clock, heartbeat_timeout=_TIMEOUT)
    session = registry.register(make_worker(worker_id=str(worker_uuid), at=_T0))

    registry.mark_disconnected(FleetWorkerId(str(worker_uuid)), session)

    adapter = _registry_adapter(registry)

    assert adapter.is_worker_connected(worker_id=WorkerId(worker_uuid)) is False


# --- holds_working_set bridges UUID server-id to the registry string (#696) --


def test_holds_working_set_reflects_reported_ids() -> None:
    worker_uuid = uuid.uuid4()
    server_uuid = uuid.uuid4()
    clock = FakeClock(_T0)
    registry = InMemoryWorkerRegistry(clock=clock, heartbeat_timeout=_TIMEOUT)
    registry.register(
        make_worker(worker_id=str(worker_uuid), at=_T0),
        held_server_ids=frozenset({str(server_uuid)}),
    )

    adapter = _registry_adapter(registry)

    assert (
        adapter.holds_working_set(
            worker_id=WorkerId(worker_uuid), server_id=ServerId(server_uuid)
        )
        is True
    )
    assert (
        adapter.holds_working_set(
            worker_id=WorkerId(worker_uuid), server_id=ServerId(uuid.uuid4())
        )
        is False
    )


# --- resource-aware placement folds committed accounting in (#710) -----------

# A 4 GiB host: 4096 MiB capacity, reserve max(1024, 410) = 1024 -> 3072 usable.
_4GIB = HostResources(cpu_cores=4, memory_bytes=4 * 1024 * 1024 * 1024)


async def test_place_excludes_worker_over_committed_on_memory() -> None:
    worker_uuid = uuid.uuid4()
    registry = InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
    registry.register(make_worker(worker_id=str(worker_uuid), resources=_4GIB, at=_T0))
    adapter = _registry_adapter(registry)

    # Committed 2048 + request 2048 = 4096 > 3072 usable -> excluded -> None.
    chosen = await adapter.place(
        backend=ExecutionBackend.HOST_PROCESS,
        memory_limit_mb=2048,
        committed_by_worker={WorkerId(worker_uuid): CommittedResources(memory_mb=2048)},
    )

    assert chosen is None


async def test_place_admits_worker_with_memory_room() -> None:
    worker_uuid = uuid.uuid4()
    registry = InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
    registry.register(make_worker(worker_id=str(worker_uuid), resources=_4GIB, at=_T0))
    adapter = _registry_adapter(registry)

    # Committed 512 + request 2048 = 2560 <= 3072 usable -> fits.
    chosen = await adapter.place(
        backend=ExecutionBackend.HOST_PROCESS,
        memory_limit_mb=2048,
        committed_by_worker={WorkerId(worker_uuid): CommittedResources(memory_mb=512)},
    )

    assert chosen == WorkerId(worker_uuid)


async def test_place_unset_request_memory_is_not_gated() -> None:
    worker_uuid = uuid.uuid4()
    registry = InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
    registry.register(make_worker(worker_id=str(worker_uuid), resources=_4GIB, at=_T0))
    adapter = _registry_adapter(registry)

    chosen = await adapter.place(
        backend=ExecutionBackend.HOST_PROCESS,
        memory_limit_mb=None,
        committed_by_worker={WorkerId(worker_uuid): CommittedResources(memory_mb=4096)},
    )

    assert chosen == WorkerId(worker_uuid)
