"""Only a stop's final snapshot is marked final on the wire (issue #4053).

The flag has the Worker end the containers its failed starts left running for a
stopped server before the snapshot packs. A periodic or on-demand snapshot may
capture a crashed server that is meant to run, whose re-sent start ends them, so
neither sets it. Each use case runs against the real servers adapter and fleet
control plane, and each test reads the ``SnapshotTrigger`` the Worker receives.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Awaitable

from mc_server_dashboard_api.fleet.adapters.control_plane import (
    ControlPlaneState,
    GrpcControlPlane,
)
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId as FleetWorkerId
from mc_server_dashboard_api.servers.adapters.control_plane import (
    FleetControlPlaneAdapter,
)
from mc_server_dashboard_api.servers.application.lifecycle import StopServer
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    RunSnapshotCadenceTick,
    SnapshotServer,
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
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from tests.fleet.fakes import FakeClock as FleetFakeClock
from tests.fleet.fakes import make_worker
from tests.servers.fakes import FakeClock, FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


def _running_server() -> Server:
    return Server(
        id=ServerId.new(),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.RUNNING,
        observed_state=ObservedState.RUNNING,
        observed_at=None,
        assigned_worker_id=WorkerId(uuid.uuid4()),
        created_at=_NOW,
        updated_at=_NOW,
    )


class _Worker:
    """The server's connected Worker, behind the real control-plane adapter."""

    def __init__(self, server: Server) -> None:
        assert server.assigned_worker_id is not None
        self._fleet_id = FleetWorkerId(str(server.assigned_worker_id.value))
        registry = InMemoryWorkerRegistry(
            clock=FleetFakeClock(_NOW), heartbeat_timeout=dt.timedelta(seconds=30)
        )
        session = registry.register(
            make_worker(worker_id=self._fleet_id.value, at=_NOW)
        )
        self._state = ControlPlaneState()
        self._queue = self._state.open_session(self._fleet_id, session)
        self.cp = FleetControlPlaneAdapter(
            registry=registry,
            control_plane=GrpcControlPlane(
                self._state, clock=FleetFakeClock(_NOW), timeout_seconds=30
            ),
            data_plane_base_url="http://data-plane.test",
            worker_credential="test-credential",
        )

    async def answer(
        self, flow: Awaitable[object], commands: int
    ) -> list[pb.ApiCommand]:
        """Run ``flow``, answering the ``commands`` it sends OK, in order."""

        task = asyncio.ensure_future(flow)
        sent = []
        for _ in range(commands):
            message = await asyncio.wait_for(self._queue.get(), timeout=1)
            sent.append(message.api_command)
            await self._state.resolve(
                message.correlation_id,
                self._fleet_id,
                pb.CommandResult(success=True),
                received_at=_NOW,
            )
        await asyncio.wait_for(task, timeout=1)
        return sent


async def test_a_stops_final_snapshot_is_marked_final() -> None:
    server = _running_server()
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    worker = _Worker(server)
    stop = StopServer(uow=uow, control_plane=worker.cp, clock=FakeClock(_NOW))

    sent = await worker.answer(
        stop(community_id=server.community_id, server_id=server.id), commands=2
    )

    assert [c.WhichOneof("command") for c in sent] == ["stop", "snapshot"]
    assert sent[1].snapshot.final is True


async def test_a_periodic_snapshot_is_not_marked_final() -> None:
    server = _running_server()
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    worker = _Worker(server)
    clock = FakeClock(_NOW)
    scheduler = RunSnapshotCadenceTick(
        uow=uow,
        control_plane=worker.cp,
        clock=clock,
        default_interval_seconds=3600,
        min_interval_seconds=300,
    )
    await scheduler.tick()  # schedules the first due instant
    clock.set(_NOW + dt.timedelta(seconds=3600))

    sent = await worker.answer(scheduler.tick(), commands=1)

    assert [c.WhichOneof("command") for c in sent] == ["snapshot"]
    assert sent[0].snapshot.final is False


async def test_an_on_demand_snapshot_is_not_marked_final() -> None:
    server = _running_server()
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    worker = _Worker(server)
    snapshot = SnapshotServer(uow=uow, control_plane=worker.cp)

    sent = await worker.answer(
        snapshot(community_id=server.community_id, server_id=server.id), commands=1
    )

    assert [c.WhichOneof("command") for c in sent] == ["snapshot"]
    assert sent[0].snapshot.final is False
