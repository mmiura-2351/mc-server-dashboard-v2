"""Use-case tests for ``StopServersAssignedToWorker`` (the drain stop, FR-WRK-5).

Draining a Worker marks every server assigned to it ``desired=stopped``; the
procedure lives here, beside ``StopServer``, because it is the same
compare-and-set flip applied to a set of servers rather than one (issue #2578).
These drive it against in-memory fakes (no DB): which servers it selects, the
per-server CAS, the clock stamp, and the ids it reports back for the caller's
placement bookkeeping.
"""

from __future__ import annotations

import datetime as dt
import uuid

from mc_server_dashboard_api.servers.application.lifecycle import (
    StopServersAssignedToWorker,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from tests.servers.fakes import FakeClock, FakeServerRepository, FakeUnitOfWork

_T0 = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_LATER = _T0 + dt.timedelta(minutes=5)
_WORKER = WorkerId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
_OTHER_WORKER = WorkerId(uuid.UUID("22222222-2222-2222-2222-222222222222"))


def _server(
    *,
    desired: DesiredState,
    worker_id: WorkerId | None,
) -> Server:
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=desired,
        observed_state=ObservedState.RUNNING,
        observed_at=None,
        assigned_worker_id=worker_id,
        created_at=_T0,
        updated_at=_T0,
    )


class _LostRaceRepository(FakeServerRepository):
    """A repository whose lifecycle CAS always loses, as a concurrent stop would."""

    async def update_lifecycle(
        self,
        server: Server,
        *,
        expected_from: DesiredState,
        require_unassigned: bool = False,
    ) -> bool:
        return False


async def test_stops_only_the_servers_assigned_to_this_worker() -> None:
    uow = FakeUnitOfWork()
    mine = _server(desired=DesiredState.RUNNING, worker_id=_WORKER)
    theirs = _server(desired=DesiredState.RUNNING, worker_id=_OTHER_WORKER)
    unassigned = _server(desired=DesiredState.RUNNING, worker_id=None)
    for server in (mine, theirs, unassigned):
        uow.servers.seed(server)

    stopped = await StopServersAssignedToWorker(uow=uow, clock=FakeClock(_LATER))(
        worker_id=_WORKER
    )

    assert stopped == [mine.id]
    assert uow.servers.by_id[mine.id].desired_state is DesiredState.STOPPED
    assert uow.servers.by_id[theirs.id].desired_state is DesiredState.RUNNING
    assert uow.servers.by_id[unassigned.id].desired_state is DesiredState.RUNNING
    assert uow.commits == 1


async def test_stamps_updated_at_and_keeps_the_assignment() -> None:
    # The assignment is left intact: the reconciler's redispatch_stop clears it on
    # the confirmed stop, the same as a single StopServer.
    uow = FakeUnitOfWork()
    server = _server(desired=DesiredState.RUNNING, worker_id=_WORKER)
    uow.servers.seed(server)

    await StopServersAssignedToWorker(uow=uow, clock=FakeClock(_LATER))(
        worker_id=_WORKER
    )

    stored = uow.servers.by_id[server.id]
    assert stored.updated_at == _LATER
    assert stored.assigned_worker_id == _WORKER


async def test_reports_no_id_for_a_server_whose_cas_lost() -> None:
    # A concurrent stop already moved the row out of running: the flip is skipped,
    # not raised, and the id is NOT reported — the caller must not shed placement
    # load for a flip that did not happen.
    uow = FakeUnitOfWork(servers=_LostRaceRepository())
    server = _server(desired=DesiredState.RUNNING, worker_id=_WORKER)
    uow.servers.seed(server)

    stopped = await StopServersAssignedToWorker(uow=uow, clock=FakeClock(_LATER))(
        worker_id=_WORKER
    )

    assert stopped == []


async def test_reports_nothing_when_the_worker_holds_no_running_server() -> None:
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(desired=DesiredState.STOPPED, worker_id=_WORKER))

    stopped = await StopServersAssignedToWorker(uow=uow, clock=FakeClock(_LATER))(
        worker_id=_WORKER
    )

    assert stopped == []
