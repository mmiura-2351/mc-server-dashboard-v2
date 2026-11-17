"""The fleet-backed report of a committed server delete to the Workers (#3679)."""

from __future__ import annotations

import datetime as dt

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.requests import Request

from mc_server_dashboard_api.dependencies import get_delete_server
from mc_server_dashboard_api.fleet.adapters.control_plane import (
    ControlPlaneState,
    GrpcControlPlane,
)
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from mc_server_dashboard_api.servers.adapters.deleted_server_reclaim import (
    FleetDeletedServerReclaim,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from tests.fleet.fakes import FakeClock, make_worker
from tests.servers.fakes import FakeBackupArchiveStore

_T0 = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_TIMEOUT = dt.timedelta(seconds=30)
_CONNECTED = (
    "11111111-1111-1111-1111-111111111111",
    "22222222-2222-2222-2222-222222222222",
)
_DISCONNECTED = "33333333-3333-3333-3333-333333333333"


async def test_reports_the_delete_to_every_connected_worker() -> None:
    # The delete has no Worker to address: a server is deleted only while
    # unassigned, and any Worker may still hold its scratch, including one the
    # API never recorded as holding it (a hydrate whose dispatch timed out). So
    # every connected Worker is told; one that holds nothing reclaims nothing.
    state = ControlPlaneState()
    control_plane = GrpcControlPlane(state, clock=FakeClock(_T0), timeout_seconds=5.0)
    registry = InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
    queues = {}
    for worker in _CONNECTED:
        session = registry.register(make_worker(worker_id=worker, at=_T0))
        queues[worker] = state.open_session(WorkerId(worker), session)
    # Registered but with no session: skipped, and its next registration's
    # unknown_held_server_ids is the backstop.
    registry.register(make_worker(worker_id=_DISCONNECTED, at=_T0))
    server_id = ServerId.new()

    await FleetDeletedServerReclaim(
        registry=registry, control_plane=control_plane
    ).reclaim(server_id)

    for worker, queue in queues.items():
        msg = queue.get_nowait()
        assert msg.api_command.server_id == str(server_id.value)
        assert msg.api_command.WhichOneof("command") == "reclaim_deleted_server"
        assert queue.empty()
        # Answer it, so the result logger the fire-and-forget dispatch started
        # finishes inside the test.
        await state.resolve(
            msg.api_command.command_id,
            WorkerId(worker),
            pb.CommandResult(success=True),
        )


async def test_the_delete_use_case_reports_through_the_fleet() -> None:
    # Pins the real construction site: an unwired report is a silent no-op that
    # leaves every connected Worker waiting for its next registration again.
    state = ControlPlaneState()
    registry = InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
    worker = _CONNECTED[0]
    session = registry.register(make_worker(worker_id=worker, at=_T0))
    queue = state.open_session(WorkerId(worker), session)
    app = FastAPI()
    app.state.engine = create_async_engine("postgresql+asyncpg://u:p@h/db")
    app.state.worker_registry = registry
    app.state.control_plane = GrpcControlPlane(
        state, clock=FakeClock(_T0), timeout_seconds=5.0
    )
    server_id = ServerId.new()
    try:
        delete = get_delete_server(
            Request(scope={"type": "http", "app": app}),
            backup_store=FakeBackupArchiveStore(),
        )
        await delete.deleted_server_reclaim.reclaim(server_id)
    finally:
        await app.state.engine.dispose()

    msg = queue.get_nowait()
    assert msg.api_command.server_id == str(server_id.value)
    assert msg.api_command.WhichOneof("command") == "reclaim_deleted_server"
    await state.resolve(
        msg.api_command.command_id, WorkerId(worker), pb.CommandResult(success=True)
    )
