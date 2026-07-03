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
    CommandTimedOutError,
    EditFileCommand,
    HydrateCommand,
    LaunchMode,
    ListFilesCommand,
    ReadFileCommand,
    SnapshotCommand,
    StartServerCommand,
    WorkerNotConnectedError,
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
        self.last_timeout_override: float | None = None
        self.last_snapshot_is_final: bool | None = None
        self._result = result or CommandResult(code=CommandResultCode.OK)

    async def dispatch(
        self,
        *,
        worker_id: FleetWorkerId,
        server_id: str,
        command: Command,
        timeout_override: float | None = None,
        snapshot_is_final: bool = False,
    ) -> CommandResult:
        self.last = command
        self.last_timeout_override = timeout_override
        self.last_snapshot_is_final = snapshot_is_final
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


async def test_hydrate_carries_the_hydrate_timeout_override() -> None:
    # The start's hydrate phase dispatches with the longer hydrate budget so a
    # large-world pull does not time out under the general command deadline (#822).
    fleet = _CapturingFleetControlPlane()
    adapter = FleetControlPlaneAdapter(
        registry=None,  # type: ignore[arg-type]  # unused by hydrate
        control_plane=fleet,
        data_plane_base_url="https://api.example/",
        worker_credential="shhh",
        hydrate_timeout_seconds=600,
    )

    await adapter.hydrate(
        worker_id=WorkerId(uuid.uuid4()),
        community_id=CommunityId(uuid.uuid4()),
        server_id=ServerId(uuid.uuid4()),
    )

    assert fleet.last_timeout_override == 600


async def test_snapshot_carries_the_snapshot_timeout_override() -> None:
    # The stop's final snapshot dispatches with the longer snapshot budget so a
    # large-world upload does not time out under the general command deadline and
    # release the held assignment mid-upload, reopening the stop->re-place race
    # (#847). Mirrors the hydrate budget (#822/#868).
    fleet = _CapturingFleetControlPlane()
    adapter = FleetControlPlaneAdapter(
        registry=None,  # type: ignore[arg-type]  # unused by snapshot
        control_plane=fleet,
        data_plane_base_url="https://api.example/",
        worker_credential="shhh",
        snapshot_timeout_seconds=600,
    )

    await adapter.snapshot(
        worker_id=WorkerId(uuid.uuid4()),
        community_id=CommunityId(uuid.uuid4()),
        server_id=ServerId(uuid.uuid4()),
    )

    assert fleet.last_timeout_override == 600


async def test_stop_carries_the_stop_timeout_override() -> None:
    # The graceful stop's worker round-trip dispatches with the longer stop budget
    # so the worker's in-container save + docker-stop escalation does not time out
    # under the general command deadline and 503 the user for a stop the worker
    # actually completes, wedging the assignment and losing the final snapshot
    # (#930). Mirrors the hydrate (#822) and snapshot (#847) budgets.
    fleet = _CapturingFleetControlPlane()
    adapter = FleetControlPlaneAdapter(
        registry=None,  # type: ignore[arg-type]  # unused by stop
        control_plane=fleet,
        data_plane_base_url="https://api.example/",
        worker_credential="shhh",
        stop_timeout_seconds=600,
    )

    await adapter.stop(
        worker_id=WorkerId(uuid.uuid4()), server_id=ServerId(uuid.uuid4())
    )

    assert fleet.last_timeout_override == 600


async def test_non_budgeted_commands_use_the_default_timeout() -> None:
    # Only the hydrate/snapshot/stop dispatches override the deadline; every other
    # command stays on the default command timeout (override is None).
    fleet = _CapturingFleetControlPlane()
    adapter = FleetControlPlaneAdapter(
        registry=None,  # type: ignore[arg-type]
        control_plane=fleet,
        data_plane_base_url="https://api.example/",
        worker_credential="shhh",
        hydrate_timeout_seconds=600,
        snapshot_timeout_seconds=600,
        stop_timeout_seconds=600,
    )

    await adapter.restart(
        worker_id=WorkerId(uuid.uuid4()), server_id=ServerId(uuid.uuid4())
    )

    assert fleet.last_timeout_override is None


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


class _RaisingFleetControlPlane(FleetControlPlane):
    """A fleet control plane whose dispatch always raises a given exception, so
    the adapter's error -> WorkerUnavailableError mapping can be tested directly."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def dispatch(
        self,
        *,
        worker_id: FleetWorkerId,
        server_id: str,
        command: Command,
        timeout_override: float | None = None,
        snapshot_is_final: bool = False,
    ) -> CommandResult:
        raise self._exc


async def test_timeout_maps_to_worker_unavailable_with_upload_may_be_live() -> None:
    # A CommandTimedOutError means the worker session is healthy and the transfer
    # is still uploading (only the API future was abandoned): the final-stop
    # snapshot must HOLD the assignment, so the flag is True (#847/#874). A direct
    # adapter assertion guards the mapping at servers/adapters/control_plane.py:425,
    # which lifecycle tests only hand-mimic — a regression dropping the flag would
    # otherwise pass the suite.
    fleet = _RaisingFleetControlPlane(CommandTimedOutError("dispatch timed out"))
    adapter = _adapter(fleet)

    with pytest.raises(WorkerUnavailableError) as excinfo:
        await adapter.snapshot(
            worker_id=WorkerId(uuid.uuid4()),
            community_id=CommunityId(uuid.uuid4()),
            server_id=ServerId(uuid.uuid4()),
        )
    assert excinfo.value.upload_may_be_live is True


async def test_disconnect_maps_to_worker_unavailable_without_upload_may_be_live() -> (
    None
):
    # A WorkerNotConnectedError means the worker is gone and the upload died with
    # its context, so the assignment is released as before: the flag is False.
    fleet = _RaisingFleetControlPlane(WorkerNotConnectedError("worker gone"))
    adapter = _adapter(fleet)

    with pytest.raises(WorkerUnavailableError) as excinfo:
        await adapter.snapshot(
            worker_id=WorkerId(uuid.uuid4()),
            community_id=CommunityId(uuid.uuid4()),
            server_id=ServerId(uuid.uuid4()),
        )
    assert excinfo.value.upload_may_be_live is False


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
    # The diagnostic names BOTH settings that can supply the URL (#1549).
    with pytest.raises(
        WorkerUnavailableError,
        match=r"neither server\.data_plane_base_url nor server\.public_base_url",
    ):
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


# --- held_generation bridges UUID server-id to the registry string (#763) ----


def test_held_generation_reflects_reported_servers() -> None:
    worker_uuid = uuid.uuid4()
    server_uuid = uuid.uuid4()
    clock = FakeClock(_T0)
    registry = InMemoryWorkerRegistry(clock=clock, heartbeat_timeout=_TIMEOUT)
    registry.register(
        make_worker(worker_id=str(worker_uuid), at=_T0),
        held_servers={str(server_uuid): 7},
    )

    adapter = _registry_adapter(registry)

    assert (
        adapter.held_generation(
            worker_id=WorkerId(worker_uuid), server_id=ServerId(server_uuid)
        )
        == 7
    )
    assert (
        adapter.held_generation(
            worker_id=WorkerId(worker_uuid), server_id=ServerId(uuid.uuid4())
        )
        is None
    )


def test_holds_fresh_working_set_mirrors_held_store_comparison() -> None:
    # holds_fresh_working_set is True exactly when held >= store (the #763
    # skip-hydrate predicate the reconciler reuses for its short grace, #999).
    worker_uuid = uuid.uuid4()
    server_uuid = uuid.uuid4()
    clock = FakeClock(_T0)
    registry = InMemoryWorkerRegistry(clock=clock, heartbeat_timeout=_TIMEOUT)
    registry.register(
        make_worker(worker_id=str(worker_uuid), at=_T0),
        held_servers={str(server_uuid): 5},
    )
    adapter = _registry_adapter(registry)
    worker_id = WorkerId(worker_uuid)
    server_id = ServerId(server_uuid)

    # held(5) >= store(5) and >= store(4): fresh enough -> skip hydrate.
    assert adapter.holds_fresh_working_set(
        worker_id=worker_id, server_id=server_id, store_generation=5
    )
    assert adapter.holds_fresh_working_set(
        worker_id=worker_id, server_id=server_id, store_generation=4
    )
    # held(5) < store(6): stale -> must hydrate.
    assert not adapter.holds_fresh_working_set(
        worker_id=worker_id, server_id=server_id, store_generation=6
    )
    # Nothing held for an unknown server -> must hydrate.
    assert not adapter.holds_fresh_working_set(
        worker_id=worker_id, server_id=ServerId(uuid.uuid4()), store_generation=0
    )


# --- resource-aware placement folds committed accounting in (#710) -----------

# A 4 GiB host: 4096 MiB capacity, reserve max(1024, 410) = 1024 -> 3072 usable.
_4GIB = HostResources(cpu_cores=4, memory_bytes=4 * 1024 * 1024 * 1024)


def _commit_memory(
    registry: InMemoryWorkerRegistry, worker_uuid: uuid.UUID, memory_mb: int
) -> None:
    # Seed committed memory registry-side (#843): a confirmed assignment carries its
    # declared memory, which the placement gate now reads from the registry rather
    # than from the DB committed_by_worker snapshot.
    fleet_id = FleetWorkerId(str(worker_uuid))
    server_id = str(uuid.uuid4())
    registry.reserve(fleet_id, server_id, memory_mb)
    registry.increment_assignment(fleet_id, server_id)


async def test_place_excludes_worker_over_committed_on_memory() -> None:
    worker_uuid = uuid.uuid4()
    registry = InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
    registry.register(make_worker(worker_id=str(worker_uuid), resources=_4GIB, at=_T0))
    _commit_memory(registry, worker_uuid, 2048)
    adapter = _registry_adapter(registry)

    # Committed 2048 + request 2048 = 4096 > 3072 usable -> excluded -> None.
    chosen = await adapter.place(
        server_id=ServerId(uuid.uuid4()),
        memory_limit_mb=2048,
        committed_by_worker={},
    )

    assert chosen is None


async def test_place_admits_worker_with_memory_room() -> None:
    worker_uuid = uuid.uuid4()
    registry = InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
    registry.register(make_worker(worker_id=str(worker_uuid), resources=_4GIB, at=_T0))
    _commit_memory(registry, worker_uuid, 512)
    adapter = _registry_adapter(registry)

    # Committed 512 + request 2048 = 2560 <= 3072 usable -> fits.
    chosen = await adapter.place(
        server_id=ServerId(uuid.uuid4()),
        memory_limit_mb=2048,
        committed_by_worker={},
    )

    assert chosen == WorkerId(worker_uuid)


async def test_place_unset_request_memory_is_not_gated() -> None:
    worker_uuid = uuid.uuid4()
    registry = InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
    registry.register(make_worker(worker_id=str(worker_uuid), resources=_4GIB, at=_T0))
    _commit_memory(registry, worker_uuid, 4096)
    adapter = _registry_adapter(registry)

    chosen = await adapter.place(
        server_id=ServerId(uuid.uuid4()),
        memory_limit_mb=None,
        committed_by_worker={},
    )

    assert chosen == WorkerId(worker_uuid)


async def test_place_cpu_tie_break_still_uses_db_committed() -> None:
    # Committed memory now comes from the registry (#843), but committed CPU is still
    # folded from the DB committed_by_worker snapshot as the soft tie-break: with two
    # equally-loaded, memory-eligible workers the one carrying less declared CPU wins.
    worker_a = uuid.uuid4()
    worker_b = uuid.uuid4()
    registry = InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
    registry.register(make_worker(worker_id=str(worker_a), resources=_4GIB, at=_T0))
    registry.register(make_worker(worker_id=str(worker_b), resources=_4GIB, at=_T0))
    adapter = _registry_adapter(registry)

    chosen = await adapter.place(
        server_id=ServerId(uuid.uuid4()),
        memory_limit_mb=512,
        committed_by_worker={
            WorkerId(worker_a): CommittedResources(cpu_millis=3000),
            WorkerId(worker_b): CommittedResources(cpu_millis=1000),
        },
    )

    assert chosen == WorkerId(worker_b)


# --- placement reservation closes the capacity race (#778) -------------------


async def test_place_reserves_so_second_placement_sees_the_last_count_slot() -> None:
    # Two placements against a worker with a single capacity slot: the first
    # reserves it at decision time, so the second sees the slot taken and gets no
    # eligible worker — the capacity race cannot oversubscribe max_servers (#778).
    worker_uuid = uuid.uuid4()
    registry = InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
    registry.register(make_worker(worker_id=str(worker_uuid), max_servers=1, at=_T0))
    adapter = _registry_adapter(registry)

    first = await adapter.place(
        server_id=ServerId(uuid.uuid4()),
        memory_limit_mb=None,
        committed_by_worker={},
    )
    second = await adapter.place(
        server_id=ServerId(uuid.uuid4()),
        memory_limit_mb=None,
        committed_by_worker={},
    )

    assert first == WorkerId(worker_uuid)
    assert second is None


async def test_place_reservation_folds_memory_into_the_gate() -> None:
    # The reserved server's declared memory counts against the next placement's
    # memory gate, so two memory-heavy starts cannot both take the last memory slot
    # (#778). 4 GiB host -> 3072 usable; a 2048 reservation leaves room only below
    # 1024 for the next request.
    worker_uuid = uuid.uuid4()
    registry = InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
    registry.register(make_worker(worker_id=str(worker_uuid), resources=_4GIB, at=_T0))
    adapter = _registry_adapter(registry)

    first = await adapter.place(
        server_id=ServerId(uuid.uuid4()),
        memory_limit_mb=2048,
        committed_by_worker={},
    )
    # 2048 reserved + 2048 requested = 4096 > 3072 usable -> excluded.
    second = await adapter.place(
        server_id=ServerId(uuid.uuid4()),
        memory_limit_mb=2048,
        committed_by_worker={},
    )

    assert first == WorkerId(worker_uuid)
    assert second is None


async def test_place_memory_gate_survives_confirm_between_snapshot_and_place() -> None:
    # #843 cross-axis race: B reads its DB committed-memory snapshot one await before
    # placing; A then commits AND confirms, popping A's reserved memory. With the old
    # split (DB snapshot + live reservations) A's memory landed in NEITHER source for
    # B, so B oversubscribed. Now committed memory is registry-side, read in B's sync
    # section, so A's confirmed 2048 is counted and B is excluded.
    worker_uuid = uuid.uuid4()
    registry = InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
    registry.register(make_worker(worker_id=str(worker_uuid), resources=_4GIB, at=_T0))
    adapter = _registry_adapter(registry)

    # B's stale committed-memory snapshot (taken before A's commit): empty.
    b_committed_snapshot: dict[WorkerId, CommittedResources] = {}

    # A places, commits, and confirms — A's 2048 moves reserved -> committed.
    a_server = ServerId(uuid.uuid4())
    chosen_a = await adapter.place(
        server_id=a_server,
        memory_limit_mb=2048,
        committed_by_worker={},
    )
    assert chosen_a == WorkerId(worker_uuid)
    adapter.increment_assignment(worker_id=WorkerId(worker_uuid), server_id=a_server)

    # B now places with its stale snapshot; the gate must still see A's 2048.
    # 2048 committed + 2048 requested = 4096 > 3072 usable -> excluded.
    chosen_b = await adapter.place(
        server_id=ServerId(uuid.uuid4()),
        memory_limit_mb=2048,
        committed_by_worker=b_committed_snapshot,
    )

    assert chosen_b is None
