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

import pytest

from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.application.set_worker_drain import SetWorkerDrain
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from mc_server_dashboard_api.servers.application.lifecycle import (
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.application.reconciler import RunReconcilerTick
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
from tests.servers.fakes import (
    FakeControlPlane,
    FakeJarProvisioner,
    FakeStoreGenerationReader,
    FakeUnitOfWork,
)

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


def _assigned_count(registry: InMemoryWorkerRegistry, worker_id: WorkerId) -> int:
    snapshot = registry.get(worker_id)
    assert snapshot is not None
    return snapshot.assigned_count


class _FailingCommitUnitOfWork(FakeUnitOfWork):
    """A FakeUnitOfWork whose commit raises, to exercise the rollback path."""

    async def commit(self) -> None:
        raise RuntimeError("forced commit failure")


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


async def test_drain_decrements_placement_load_per_stopped_server() -> None:
    # I1: drain owns the desired=running -> stopped flip, so it owns the placement
    # decrement that pairs with it (mirroring StopServer.__call__). Without this the
    # registry load stays inflated until the Worker reconnects and the tally is
    # rebuilt, skewing GET /workers and placement.
    uow = FakeUnitOfWork()
    s = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
        worker_uuid=_WORKER_UUID,
    )
    uow.servers.seed(s)
    registry = _registry()
    worker_id = WorkerId(str(_WORKER_UUID))
    # Seed the worker's committed load to 1 (one assigned, desired-running server),
    # the truth the drain is about to flip.
    registry.set_assignment(
        worker_id, {str(s.id.value): 0}, registry.assignment_epoch(worker_id)
    )
    assert _assigned_count(registry, worker_id) == 1

    count = await _use_case(registry, uow)(worker_id=worker_id, draining=True)

    assert count == 1
    # The decrement (run after the commit lands) dropped the load to 0 within the
    # PUT, without waiting on a reconnect rebuild.
    assert _assigned_count(registry, worker_id) == 0


async def test_drain_does_not_decrement_when_commit_fails() -> None:
    # I1 (round 2): the decrement runs AFTER commit, so a failed commit (which
    # rolls back the CAS flips) must NOT leak decrements. Otherwise the registry
    # load would understate the still-running assignment until a reconnect rebuild.
    uow = _FailingCommitUnitOfWork()
    s = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
        worker_uuid=_WORKER_UUID,
    )
    uow.servers.seed(s)
    registry = _registry()
    worker_id = WorkerId(str(_WORKER_UUID))
    registry.set_assignment(
        worker_id, {str(s.id.value): 0}, registry.assignment_epoch(worker_id)
    )
    assert _assigned_count(registry, worker_id) == 1

    with pytest.raises(RuntimeError, match="forced commit failure"):
        await _use_case(registry, uow)(worker_id=worker_id, draining=True)

    # The CAS flips rolled back with the commit; the load stays at its pre-drain
    # value rather than leaking a decrement.
    assert _assigned_count(registry, worker_id) == 1


async def test_drain_converges_through_reconciler_with_final_snapshot() -> None:
    # B1: prove the drain use case + the reconciler tick (driven against the SAME
    # uow) produce the final snapshot by composition (#845 holds the stop scratch,
    # #849 gave redispatch_stop the snapshot leg). Drain marks desired=stopped while
    # observed=running; the reconciler then redispatch_stops, and that stop now
    # dispatches the post-stop final snapshot to the assigned Worker.
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
    # Drain only marked the intent; nothing has been dispatched to the Worker yet.
    assert uow.servers.by_id[s.id].desired_state is DesiredState.STOPPED
    assert uow.servers.by_id[s.id].observed_state is ObservedState.RUNNING

    # Run a reconciler tick well past the grace window against the same fakes.
    now = _T0 + dt.timedelta(seconds=3600)
    cp = FakeControlPlane()
    clock = ServersFakeClock(now)
    reconciler = RunReconcilerTick(
        uow=uow,
        make_start_server=lambda: StartServer(
            uow=uow,
            control_plane=cp,
            clock=clock,
            jar_provisioner=FakeJarProvisioner(),
            store_generation=FakeStoreGenerationReader(),
        ),
        make_stop_server=lambda: StopServer(uow=uow, control_plane=cp, clock=clock),
        control_plane=cp,
        clock=clock,
        grace_seconds=60,
        backoff_base_seconds=30,
        backoff_max_seconds=3600,
    )
    await reconciler.tick()

    # The reconciler stopped the drain-marked server AND took the final snapshot,
    # both targeting the assigned Worker (the FR-WRK-5 promise the review's B1 said
    # was missing). Convergence also cleared the assignment. The control plane
    # records the server-domain WorkerId the reconciler dispatched to.
    server_worker = ServersWorkerId(_WORKER_UUID)
    assert ("stop", server_worker, s.id) in cp.dispatched
    assert ("snapshot", server_worker, s.id) in cp.dispatched
    assert uow.servers.by_id[s.id].assigned_worker_id is None
