"""Tests for the servers control-plane adapter's data-plane URL building (#106).

The adapter must address the data-plane endpoint for a ``(community, server)``
scope and hand the Worker the URL + the shared credential as the transfer token.
"""

from __future__ import annotations

import uuid

import pytest

from mc_server_dashboard_api.fleet.domain.control_plane import (
    Command,
    CommandResult,
    CommandResultCode,
    EditFileCommand,
    HydrateCommand,
    ReadFileCommand,
    SnapshotCommand,
)
from mc_server_dashboard_api.fleet.domain.control_plane import (
    ControlPlane as FleetControlPlane,
)
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId as FleetWorkerId
from mc_server_dashboard_api.servers.adapters.control_plane import (
    FleetControlPlaneAdapter,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandStatus,
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
    WorkerId,
)


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
        f"https://api.example/data-plane/communities/{community}"
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
        f"https://api.example/data-plane/communities/{community}"
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
