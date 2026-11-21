"""A start's wait for its Worker's uploads and the hydrate budget (issue #3950).

Before a start hydrates a Worker, it waits for that Worker's snapshot uploads of
the server still in flight. The wait and the hydrate share one deadline, the
hydrate budget from the moment the wait begins: the hydrate gets what the wait
left. A wait that leaves nothing sends nothing to the Worker, and every entry
point keeps the server on it for the reconciler to retry there: nothing is
compensated, released or placed again. The data-plane side, with a real upload,
is in ``tests/dataplane/test_start_waits_for_worker_uploads.py``.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.servers.application.lifecycle import StartServer
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
)
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
from tests.servers.contract_table import worker_message
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
_BUDGET = 600.0


class _Uploads(FakeStoreGenerationReader):
    """A store whose wait for the Worker's uploads takes ``takes`` seconds.

    The wait moves ``clock`` on by that much, then answers ``settled``.
    """

    def __init__(self, clock: FakeClock, *, takes: float, settled: bool) -> None:
        super().__init__(generation=1)
        self._clock = clock
        self._takes = takes
        self._settled = settled
        self.waited: list[tuple[WorkerId, float]] = []

    async def uploads_settled(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        worker_id: WorkerId,
        timeout: float,
    ) -> bool:
        self.waited.append((worker_id, timeout))
        self._clock.set(self._clock.now() + dt.timedelta(seconds=self._takes))
        return self._settled


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
    uow: FakeUnitOfWork, cp: FakeControlPlane, reader: _Uploads, clock: FakeClock
) -> StartServer:
    return StartServer(
        uow=uow,
        control_plane=cp,
        clock=clock,
        jar_provisioner=FakeJarProvisioner(),
        store_generation=reader,
        file_store=FakeFileStore(seed_eula=True),
    )


def _kinds(cp: FakeControlPlane) -> list[str]:
    return [kind for kind, _, _ in cp.dispatched]


async def test_a_wait_that_uses_part_of_the_budget_leaves_the_hydrate_the_rest() -> (
    None
):
    server = _server(desired=DesiredState.STOPPED, on_worker=False)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(place_to=_WORKER, hydrate_timeout_seconds=_BUDGET)
    clock = FakeClock(_NOW)
    reader = _Uploads(clock, takes=400, settled=True)

    await _start(uow, cp, reader, clock)(
        community_id=server.community_id, server_id=server.id
    )

    assert reader.waited == [(_WORKER, _BUDGET)]
    assert _kinds(cp) == ["hydrate", "start"]
    assert cp.hydrate_timeouts == [_BUDGET - 400]


@pytest.mark.parametrize("settled", [False, True], ids=["ran-out", "landed-at-it"])
async def test_a_wait_that_uses_the_whole_budget_sends_no_hydrate(
    settled: bool,
) -> None:
    # The wait ran out at the budget, or the uploads left just as it did: either
    # way nothing of the budget is left for a hydrate.
    server = _server(desired=DesiredState.STOPPED, on_worker=False)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(place_to=_WORKER, hydrate_timeout_seconds=_BUDGET)
    clock = FakeClock(_NOW)
    reader = _Uploads(clock, takes=_BUDGET, settled=settled)

    with pytest.raises(SnapshotUploadInFlightError):
        await _start(uow, cp, reader, clock)(
            community_id=server.community_id, server_id=server.id
        )

    assert reader.waited == [(_WORKER, _BUDGET)]
    assert cp.dispatched == []


async def test_a_start_keeps_its_placement_when_the_wait_runs_out() -> None:
    server = _server(desired=DesiredState.STOPPED, on_worker=False)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(place_to=_WORKER, hydrate_timeout_seconds=_BUDGET)
    clock = FakeClock(_NOW)
    reader = _Uploads(clock, takes=_BUDGET, settled=False)

    with pytest.raises(SnapshotUploadInFlightError):
        await _start(uow, cp, reader, clock)(
            community_id=server.community_id, server_id=server.id
        )

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
    cp = FakeControlPlane(place_to=_WORKER, hydrate_timeout_seconds=_BUDGET)
    clock = FakeClock(_NOW)
    reader = _Uploads(clock, takes=_BUDGET, settled=False)

    with pytest.raises(SnapshotUploadInFlightError):
        await _start(uow, cp, reader, clock).place_and_start(
            community_id=server.community_id, server_id=server.id
        )

    assert reader.waited == [(_WORKER, _BUDGET)]
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
    cp = FakeControlPlane(hydrate_timeout_seconds=_BUDGET)
    clock = FakeClock(_NOW)
    reader = _Uploads(clock, takes=_BUDGET, settled=False)

    with pytest.raises(SnapshotUploadInFlightError):
        await _start(uow, cp, reader, clock).redispatch_start(
            community_id=server.community_id, server_id=server.id
        )

    assert reader.waited == [(_WORKER, _BUDGET)]
    assert cp.dispatched == []
    assert (cp.incremented, cp.released, cp.decremented) == ([], [], [])
    row = uow.servers.by_id[server.id]
    assert (row.desired_state, row.assigned_worker_id, row.observed_state) == (
        DesiredState.RUNNING,
        _WORKER,
        ObservedState.STOPPED,
    )


async def test_a_replayed_start_keeps_its_worker_as_it_was_when_the_wait_runs_out() -> (
    None
):
    # Issue #2499's replay: the start skipped the hydrate, the Worker refused it
    # for a working set it does not hold, and the replay's wait runs out. A start
    # was sent, so the server is kept as after any start sent: nothing is undone,
    # and no observed state is written over what the Worker may report.
    server = _server(desired=DesiredState.STOPPED, on_worker=False)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(
        place_to=_WORKER,
        held={(_WORKER, server.id): 1},
        outcomes={
            "start": CommandOutcome(
                status=CommandStatus.SERVER_NOT_FOUND,
                message=worker_message("working_set_absent.launch")
                % "/var/lib/mcsd/scratch/s",
            )
        },
        hydrate_timeout_seconds=_BUDGET,
    )
    clock = FakeClock(_NOW)
    reader = _Uploads(clock, takes=_BUDGET, settled=False)

    with pytest.raises(SnapshotUploadInFlightError):
        await _start(uow, cp, reader, clock)(
            community_id=server.community_id, server_id=server.id
        )

    assert _kinds(cp) == ["start"]
    assert reader.waited == [(_WORKER, _BUDGET)]
    assert (cp.incremented, cp.released, cp.decremented) == ([_WORKER], [], [])
    row = uow.servers.by_id[server.id]
    assert (
        row.desired_state,
        row.assigned_worker_id,
        row.observed_state,
        row.awaiting_hydrate,
    ) == (DesiredState.RUNNING, _WORKER, ObservedState.STOPPED, True)
