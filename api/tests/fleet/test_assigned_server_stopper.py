"""Adapter tests for the fleet ``AssignedServerStopper`` -> servers binding (#2578).

The drain flow needs the servers a Worker holds marked ``desired=stopped``, which
is a servers-context procedure. The fleet context declares the ``AssignedServerStopper``
Port and this adapter — bound only in the wiring — fulfils it against the servers
``StopServersAssignedToWorker`` use case, so nothing in ``fleet.domain`` or
``fleet.application`` imports the servers context. The bridge the adapter owns is
the identifier translation: the two contexts each have their own ``WorkerId``, and
the servers side persists it as a UUID (#93).
"""

from __future__ import annotations

import datetime as dt
import uuid

from mc_server_dashboard_api.fleet.adapters.assigned_server_stopper import (
    ServersAssignedServerStopper,
)
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
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
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    WorkerId as ServersWorkerId,
)
from tests.servers.fakes import FakeClock, FakeUnitOfWork

_T0 = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_WORKER_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _server(*, worker_uuid: uuid.UUID | None) -> Server:
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.RUNNING,
        observed_state=ObservedState.RUNNING,
        observed_at=None,
        assigned_worker_id=(
            None if worker_uuid is None else ServersWorkerId(worker_uuid)
        ),
        created_at=_T0,
        updated_at=_T0,
    )


def _stopper(uow: FakeUnitOfWork) -> ServersAssignedServerStopper:
    return ServersAssignedServerStopper(
        stop_servers=StopServersAssignedToWorker(uow=uow, clock=FakeClock(_T0))
    )


async def test_fleet_worker_id_round_trips_to_the_servers_worker_id() -> None:
    # The fleet id is an opaque string, the servers one a UUID: the adapter is the
    # only place the two meet, and a server assigned under the servers id must be
    # found by the fleet id that spells the same UUID.
    uow = FakeUnitOfWork()
    server = _server(worker_uuid=_WORKER_UUID)
    uow.servers.seed(server)

    stopped = await _stopper(uow).stop_assigned(WorkerId(str(_WORKER_UUID)))

    assert stopped == [str(server.id.value)]
    assert uow.servers.by_id[server.id].desired_state is DesiredState.STOPPED


async def test_non_uuid_worker_id_stops_nothing() -> None:
    # A Worker that never registered with a UUID-format id can hold no assigned
    # servers, so the unparsable id simply matches nothing (no error, no write).
    uow = FakeUnitOfWork()
    server = _server(worker_uuid=_WORKER_UUID)
    uow.servers.seed(server)

    stopped = await _stopper(uow).stop_assigned(WorkerId("not-a-uuid"))

    assert stopped == []
    assert uow.servers.by_id[server.id].desired_state is DesiredState.RUNNING
    assert uow.commits == 0
