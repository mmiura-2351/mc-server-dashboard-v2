"""Use-case tests for SetWorkerDrain's stop-on-drain orchestration (FR-WRK-5).

Drain flips the registry flag AND marks every assigned, desired-running server
``desired=stopped`` (the reconciler's redispatch_stop then drives the actual
graceful stop + final snapshot). These drive the use case against in-memory fakes
(no DB, no gRPC): the per-server CAS, idempotent re-drain, the returned count, and
that un-drain does NOT resurrect desired=running.
"""

from __future__ import annotations

import datetime as dt
import uuid

from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.application.set_worker_drain import SetWorkerDrain
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    WorkerId as ServersWorkerId,
)
from tests.fleet.fakes import FakeClock, make_worker
from tests.servers.fakes import FakeClock as ServersFakeClock
from tests.servers.fakes import FakeUnitOfWork

_T0 = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_WORKER_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_UUID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _server(
    *,
    desired: DesiredState,
    observed: ObservedState,
    worker_uuid: uuid.UUID | None,
) -> Server:
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.HOST_PROCESS,
        config={},
        desired_state=desired,
        observed_state=observed,
        observed_at=None,
        assigned_worker_id=(
            None if worker_uuid is None else ServersWorkerId(worker_uuid)
        ),
        created_at=_T0,
        updated_at=_T0,
    )


def _registry() -> InMemoryWorkerRegistry:
    registry = InMemoryWorkerRegistry(
        clock=FakeClock(_T0), heartbeat_timeout=dt.timedelta(seconds=30)
    )
    registry.register(make_worker(worker_id=str(_WORKER_UUID), at=_T0))
    return registry


def _use_case(registry: InMemoryWorkerRegistry, uow: FakeUnitOfWork) -> SetWorkerDrain:
    return SetWorkerDrain(registry=registry, uow=uow, clock=ServersFakeClock(_T0))


async def test_drain_stops_assigned_running_servers_only() -> None:
    uow = FakeUnitOfWork()
    on_worker = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
        worker_uuid=_WORKER_UUID,
    )
    other_worker = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
        worker_uuid=_OTHER_UUID,
    )
    unassigned = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.STOPPED,
        worker_uuid=None,
    )
    for s in (on_worker, other_worker, unassigned):
        uow.servers.seed(s)

    count = await _use_case(_registry(), uow)(
        worker_id=WorkerId(str(_WORKER_UUID)), draining=True
    )

    assert count == 1
    assert uow.servers.by_id[on_worker.id].desired_state is DesiredState.STOPPED
    # A server on a different worker and an unassigned one are untouched.
    assert uow.servers.by_id[other_worker.id].desired_state is DesiredState.RUNNING
    assert uow.servers.by_id[unassigned.id].desired_state is DesiredState.STOPPED


async def test_drain_keeps_assignment_for_reconciler_to_clear() -> None:
    # Drain only flips desired=stopped; the assignment is left for the reconciler's
    # redispatch_stop to clear on the confirmed stop (the convergence path).
    uow = FakeUnitOfWork()
    s = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
        worker_uuid=_WORKER_UUID,
    )
    uow.servers.seed(s)

    await _use_case(_registry(), uow)(
        worker_id=WorkerId(str(_WORKER_UUID)), draining=True
    )

    assert uow.servers.by_id[s.id].assigned_worker_id == ServersWorkerId(_WORKER_UUID)


async def test_drain_skips_already_stopped_server() -> None:
    uow = FakeUnitOfWork()
    already_stopped = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.STOPPED,
        worker_uuid=_WORKER_UUID,
    )
    uow.servers.seed(already_stopped)

    count = await _use_case(_registry(), uow)(
        worker_id=WorkerId(str(_WORKER_UUID)), draining=True
    )

    # list_running_assigned excludes desired=stopped, so nothing is flipped.
    assert count == 0


async def test_redrain_is_idempotent() -> None:
    uow = FakeUnitOfWork()
    s = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
        worker_uuid=_WORKER_UUID,
    )
    uow.servers.seed(s)
    registry = _registry()
    use_case = _use_case(registry, uow)

    first = await use_case(worker_id=WorkerId(str(_WORKER_UUID)), draining=True)
    second = await use_case(worker_id=WorkerId(str(_WORKER_UUID)), draining=True)

    assert first == 1
    # The second drain finds the server already desired=stopped, so it flips none.
    assert second == 0
    assert uow.servers.by_id[s.id].desired_state is DesiredState.STOPPED


async def test_undrain_does_not_resurrect_desired_running() -> None:
    uow = FakeUnitOfWork()
    s = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
        worker_uuid=_WORKER_UUID,
    )
    uow.servers.seed(s)
    registry = _registry()
    use_case = _use_case(registry, uow)

    await use_case(worker_id=WorkerId(str(_WORKER_UUID)), draining=True)
    count = await use_case(worker_id=WorkerId(str(_WORKER_UUID)), draining=False)

    # Un-drain only re-enables placement: it flips no server and leaves the
    # drain-stopped server desired=stopped.
    assert count == 0
    assert uow.servers.by_id[s.id].desired_state is DesiredState.STOPPED


async def test_drain_unknown_worker_returns_none() -> None:
    uow = FakeUnitOfWork()

    result = await _use_case(_registry(), uow)(
        worker_id=WorkerId("ghost"), draining=True
    )

    assert result is None
