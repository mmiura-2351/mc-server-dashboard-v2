"""Releasing a held server without its final snapshot (issue #4055, #3697 P3).

A stop keeps its Worker assigned until the final snapshot settles. When that
Worker refuses the snapshot ``BUSY`` or ``UNAVAILABLE``, the row records the
refusal (``final_snapshot_refusal``) and stays held. The release is the
operator's explicit way out: it drops the assignment as the stop would have,
without the snapshot and without any command to the Worker. It is scoped to that
hold, so every other row is ``invalid_transition``, and it never lands while a
stop convergence or a final snapshot of the server is in flight
(``transition_conflict``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import logging
import uuid

import pytest
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import create_async_engine

from mc_server_dashboard_api import dependencies
from mc_server_dashboard_api.dependencies import get_release_held_server
from mc_server_dashboard_api.fleet.adapters.control_plane import (
    ControlPlaneState,
    GrpcControlPlane,
)
from mc_server_dashboard_api.fleet.adapters.real_time_events import (
    InProcessRealTimeEvents,
)
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId as FleetWorkerId
from mc_server_dashboard_api.servers.adapters.control_plane import (
    FleetControlPlaneAdapter,
)
from mc_server_dashboard_api.servers.application.release_held_server import (
    ReleaseHeldServer,
)
from mc_server_dashboard_api.servers.application.stop_convergence import (
    StopConvergenceGuard,
)
from mc_server_dashboard_api.servers.application.unannounced_refusals import (
    UnannouncedRefusals,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidLifecycleTransitionError,
    LifecycleTransitionConflictError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    FinalSnapshotRefusal,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from tests.fleet.fakes import FakeClock as FleetFakeClock
from tests.fleet.fakes import make_worker
from tests.servers.fakes import FakeServerRepository, FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_LONG_AGO = _NOW - dt.timedelta(hours=1)


def _held(**changes: object) -> Server:
    """A stop held by a refused final snapshot; ``changes`` alter one field."""

    server = Server(
        id=ServerId(uuid.uuid4()),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=_LONG_AGO,
        assigned_worker_id=WorkerId(uuid.uuid4()),
        created_at=_LONG_AGO,
        updated_at=_LONG_AGO,
        awaiting_hydrate=True,
        final_snapshot_refusal=FinalSnapshotRefusal.UNAVAILABLE,
    )
    return dataclasses.replace(server, **changes)  # type: ignore[arg-type]


def _release(
    repo: FakeServerRepository, guard: StopConvergenceGuard | None = None
) -> ReleaseHeldServer:
    return ReleaseHeldServer(
        uow=FakeUnitOfWork(servers=repo),
        stop_convergence=guard or StopConvergenceGuard(),
        unannounced_refusals=UnannouncedRefusals(),
    )


def _commits(release: ReleaseHeldServer) -> int:
    assert isinstance(release.uow, FakeUnitOfWork)
    return release.uow.commits


def _seeded(server: Server) -> FakeServerRepository:
    repo = FakeServerRepository()
    repo.seed(server)
    return repo


@pytest.mark.parametrize(
    "observed",
    [ObservedState.STOPPED, ObservedState.CRASHED, ObservedState.UNKNOWN],
    ids=lambda state: state.value,
)
@pytest.mark.parametrize(
    "refusal", list(FinalSnapshotRefusal), ids=lambda refusal: refusal.value
)
async def test_a_held_server_is_released_without_its_final_snapshot(
    observed: ObservedState, refusal: FinalSnapshotRefusal
) -> None:
    # ``unknown`` is a held row whose Worker then dropped: still the hold the
    # operator is given a way out of.
    server = _held(observed_state=observed, final_snapshot_refusal=refusal)
    repo = _seeded(server)
    release = _release(repo)

    released = await release(community_id=server.community_id, server_id=server.id)

    assert _commits(release) == 1
    row = repo.by_id[server.id]
    assert row.assigned_worker_id is None
    assert row.final_snapshot_refusal is None
    assert row.awaiting_hydrate is False
    # The release leaves the observed state and the intent as they were.
    assert row.observed_state is observed
    assert row.observed_at == _LONG_AGO
    assert row.desired_state is DesiredState.STOPPED
    # The returned server reads like the row the release wrote (issue #4056).
    assert released.assigned_worker_id is None
    assert released.final_snapshot_refusal is None
    assert released.awaiting_hydrate is False
    assert released.observed_state is observed


async def test_a_release_is_logged_as_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = _held()
    assert server.assigned_worker_id is not None

    with caplog.at_level(logging.WARNING):
        await _release(_seeded(server))(
            community_id=server.community_id, server_id=server.id
        )

    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    message = record.getMessage()
    assert str(server.id.value) in message
    assert str(server.assigned_worker_id.value) in message
    assert "without its final snapshot" in message


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"desired_state": DesiredState.RUNNING}, id="running-intent"),
        pytest.param({"assigned_worker_id": None}, id="unassigned"),
        pytest.param({"final_snapshot_refusal": None}, id="no-refusal"),
        pytest.param({"observed_state": ObservedState.RUNNING}, id="observed-running"),
        pytest.param(
            {"observed_state": ObservedState.STOPPING}, id="observed-stopping"
        ),
        pytest.param(
            {"observed_state": ObservedState.STARTING}, id="observed-starting"
        ),
    ],
)
async def test_a_server_not_held_by_a_refusal_is_not_released(
    changes: dict[str, object],
) -> None:
    # Each row is the releasable one with one field changed. A row held for
    # another reason (a snapshot not answered yet, a disconnected Worker's
    # grace) records no refusal, and may still get its snapshot.
    server = _held(**changes)
    repo = _seeded(server)
    before = dataclasses.replace(repo.by_id[server.id])
    release = _release(repo)

    with pytest.raises(InvalidLifecycleTransitionError):
        await release(community_id=server.community_id, server_id=server.id)

    assert repo.by_id[server.id] == before
    assert _commits(release) == 0


async def test_a_server_of_another_community_is_not_found() -> None:
    server = _held()
    repo = _seeded(server)
    before = dataclasses.replace(repo.by_id[server.id])
    release = _release(repo)

    with pytest.raises(ServerNotFoundError):
        await release(community_id=CommunityId(uuid.uuid4()), server_id=server.id)

    assert repo.by_id[server.id] == before
    assert _commits(release) == 0


async def test_a_release_refuses_while_a_stop_convergence_holds_the_claim() -> None:
    # A stop, a reconciler convergence or a late result is running for the
    # server: it may be about to take the very snapshot the release gives up.
    server = _held()
    repo = _seeded(server)
    before = dataclasses.replace(repo.by_id[server.id])
    guard = StopConvergenceGuard()
    release = _release(repo, guard)

    with guard.claim(server.id):
        with pytest.raises(LifecycleTransitionConflictError):
            await release(community_id=server.community_id, server_id=server.id)

    assert repo.by_id[server.id] == before
    assert _commits(release) == 0


class _ReportLandsAfterTheRead(FakeServerRepository):
    """A Worker report lands between the release's read and its write.

    The read is the release's second, under the claim: the first is its
    community check (issue #4077).
    """

    def __init__(self) -> None:
        super().__init__()
        self._reads = 0

    async def get_by_id(self, server_id: ServerId) -> Server | None:
        server = await super().get_by_id(server_id)
        self._reads += 1
        if self._reads == 2:
            row = self.by_id[server_id]
            row.observed_state = ObservedState.RUNNING
            row.observed_at = _NOW
        return server


async def test_a_release_refuses_a_server_whose_state_moved_before_the_write() -> None:
    # The Worker reports the server running again after the release read it at
    # rest: the read no longer describes the row, so nothing is released.
    server = _held()
    repo = _ReportLandsAfterTheRead()
    repo.seed(server)
    release = _release(repo)

    with pytest.raises(LifecycleTransitionConflictError):
        await release(community_id=server.community_id, server_id=server.id)

    assert _commits(release) == 0

    row = repo.by_id[server.id]
    assert row.assigned_worker_id == server.assigned_worker_id
    assert row.final_snapshot_refusal is FinalSnapshotRefusal.UNAVAILABLE


class _Worker:
    """One connected Worker behind the real control-plane state and adapter."""

    def __init__(self, server: Server) -> None:
        assert server.assigned_worker_id is not None
        self.fleet_id = FleetWorkerId(str(server.assigned_worker_id.value))
        registry = InMemoryWorkerRegistry(
            clock=FleetFakeClock(_NOW), heartbeat_timeout=dt.timedelta(seconds=30)
        )
        session = registry.register(make_worker(worker_id=self.fleet_id.value, at=_NOW))
        self.state = ControlPlaneState()
        self.queue = self.state.open_session(self.fleet_id, session)
        self.guard = StopConvergenceGuard(
            outstanding=lambda sid: self.state.final_snapshot_outstanding(
                str(sid.value)
            )
        )
        self.cp = FleetControlPlaneAdapter(
            registry=registry,
            control_plane=GrpcControlPlane(
                self.state, clock=FleetFakeClock(_NOW), timeout_seconds=30
            ),
            data_plane_base_url="http://data-plane.test",
            worker_credential="test-credential",
        )


async def test_a_release_refuses_while_a_final_snapshot_is_outstanding() -> None:
    # A final snapshot of the server is on the Worker with no claim held: its
    # upload may still publish, and releasing under it would let a start on
    # another Worker hydrate the older generation (issue #847).
    server = _held()
    assert server.assigned_worker_id is not None
    repo = _seeded(server)
    before = dataclasses.replace(repo.by_id[server.id])
    worker = _Worker(server)
    snapshot = asyncio.create_task(
        worker.cp.snapshot(
            worker_id=server.assigned_worker_id,
            community_id=server.community_id,
            server_id=server.id,
            final=True,
        )
    )
    sent = await asyncio.wait_for(worker.queue.get(), timeout=1)

    with pytest.raises(LifecycleTransitionConflictError):
        await _release(repo, worker.guard)(
            community_id=server.community_id, server_id=server.id
        )

    assert repo.by_id[server.id] == before
    await worker.state.resolve(
        sent.correlation_id,
        worker.fleet_id,
        pb.CommandResult(
            success=False,
            error=pb.CommandError(code=pb.COMMAND_ERROR_CODE_UNAVAILABLE),
        ),
        received_at=_NOW,
    )
    await snapshot

    # Once the snapshot has answered, the release lands, and sends the Worker
    # nothing.
    await _release(repo, worker.guard)(
        community_id=server.community_id, server_id=server.id
    )

    assert repo.by_id[server.id].assigned_worker_id is None
    assert worker.queue.empty()


async def test_the_http_release_shares_the_app_guard_bus_and_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The in-flight check and the release frame only work on the app's guard and
    # bus; a construction site left on a private default fails silently.
    buses: list[object] = []

    def _unit_of_work(_factory: object, events: object) -> FakeUnitOfWork:
        buses.append(events)
        return FakeUnitOfWork()

    monkeypatch.setattr(dependencies, "ServersUnitOfWork", _unit_of_work)
    app = FastAPI()
    app.state.engine = create_async_engine("postgresql+asyncpg://u:p@h/db")
    app.state.real_time_events = InProcessRealTimeEvents()
    app.state.stop_convergence = StopConvergenceGuard()
    app.state.unannounced_refusals = UnannouncedRefusals()
    request = Request(scope={"type": "http", "app": app})

    try:
        release = get_release_held_server(request)
    finally:
        await app.state.engine.dispose()

    assert release.stop_convergence is app.state.stop_convergence
    # The stops' record of a refusal written unannounced, which the release
    # forgets (issue #4152).
    assert release.unannounced_refusals is app.state.unannounced_refusals
    assert buses == [app.state.real_time_events]
