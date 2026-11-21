"""A start whose wait for its Worker's uploads runs out (issue #3950).

Before a start hydrates a Worker, it waits for that Worker's snapshot uploads of
the server still in flight. When the wait runs out, nothing is sent to the
Worker, and every entry point keeps the server on it for the reconciler to retry
there: nothing is compensated, released or placed again. The data-plane side, with
a real upload, is in ``tests/dataplane/test_start_waits_for_worker_uploads.py``.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.servers.application.lifecycle import StartServer
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import SnapshotUploadInFlightError
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from tests.servers.fakes import (
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeStoreGenerationReader,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 11, 21, 12, 0, tzinfo=dt.timezone.utc)
_WORKER = WorkerId(uuid.uuid4())


class _UploadStillInFlight(FakeStoreGenerationReader):
    """A store whose wait for the Worker's uploads always runs out."""

    def __init__(self) -> None:
        super().__init__(generation=1)
        self.waited: list[WorkerId] = []

    async def uploads_settled(
        self, *, community_id: CommunityId, server_id: ServerId, worker_id: WorkerId
    ) -> bool:
        self.waited.append(worker_id)
        return False


def _server(*, desired: DesiredState, on_worker: bool) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=desired,
        observed_state=ObservedState.STOPPED,
        observed_at=_NOW - dt.timedelta(minutes=1),
        assigned_worker_id=_WORKER if on_worker else None,
        created_at=_NOW,
        updated_at=_NOW - dt.timedelta(minutes=1),
    )


def _start(
    uow: FakeUnitOfWork, cp: FakeControlPlane, reader: _UploadStillInFlight
) -> StartServer:
    return StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=reader,
        file_store=FakeFileStore(seed_eula=True),
    )


async def test_a_start_keeps_its_placement_when_the_wait_runs_out() -> None:
    server = _server(desired=DesiredState.STOPPED, on_worker=False)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(place_to=_WORKER)
    reader = _UploadStillInFlight()

    with pytest.raises(SnapshotUploadInFlightError):
        await _start(uow, cp, reader)(
            community_id=server.community_id, server_id=server.id
        )

    assert reader.waited == [_WORKER]
    assert cp.dispatched == []
    assert (cp.incremented, cp.released, cp.decremented) == ([_WORKER], [], [])
    row = uow.servers.by_id[server.id]
    assert (row.desired_state, row.assigned_worker_id, row.observed_state) == (
        DesiredState.RUNNING,
        _WORKER,
        ObservedState.UNKNOWN,
    )


async def test_an_orphan_placement_keeps_its_worker_when_the_wait_runs_out() -> None:
    server = _server(desired=DesiredState.RUNNING, on_worker=False)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(place_to=_WORKER)
    reader = _UploadStillInFlight()

    with pytest.raises(SnapshotUploadInFlightError):
        await _start(uow, cp, reader).place_and_start(
            community_id=server.community_id, server_id=server.id
        )

    assert reader.waited == [_WORKER]
    assert cp.dispatched == []
    assert (cp.incremented, cp.released, cp.decremented) == ([_WORKER], [], [])
    row = uow.servers.by_id[server.id]
    assert (row.desired_state, row.assigned_worker_id, row.observed_state) == (
        DesiredState.RUNNING,
        _WORKER,
        ObservedState.UNKNOWN,
    )


async def test_a_redispatch_changes_nothing_when_the_wait_runs_out() -> None:
    server = _server(desired=DesiredState.RUNNING, on_worker=True)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane()
    reader = _UploadStillInFlight()

    with pytest.raises(SnapshotUploadInFlightError):
        await _start(uow, cp, reader).redispatch_start(
            community_id=server.community_id, server_id=server.id
        )

    assert reader.waited == [_WORKER]
    assert cp.dispatched == []
    assert (cp.incremented, cp.released, cp.decremented) == ([], [], [])
    row = uow.servers.by_id[server.id]
    assert (row.desired_state, row.assigned_worker_id, row.observed_state) == (
        DesiredState.RUNNING,
        _WORKER,
        ObservedState.STOPPED,
    )
