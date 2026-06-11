"""Use-case tests for the server lifecycle (start/stop/restart/RCON, Section 6.5).

Drives the use cases against in-memory fakes (no DB, no gRPC): the transition
matrix (start-when-running, stop-when-stopped, restart-when-stopped all conflict),
placement failure (no eligible worker), dispatch-failure compensation (the
committed start intent is honestly reverted), RCON forwarding (only when observed
running), and the placement-load increment/decrement bookkeeping.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid

import pytest

from mc_server_dashboard_api.fleet.domain.control_plane import (
    Command as FleetCommand,
)
from mc_server_dashboard_api.fleet.domain.control_plane import (
    CommandResult,
    CommandResultCode,
    CommandTimedOutError,
    WorkerNotConnectedError,
)
from mc_server_dashboard_api.fleet.domain.control_plane import (
    ControlPlane as FleetControlPlane,
)
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId as FleetWorkerId
from mc_server_dashboard_api.servers.adapters.control_plane import (
    FleetControlPlaneAdapter,
)
from mc_server_dashboard_api.servers.application.lifecycle import (
    RestartServer,
    SendServerCommand,
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.domain.committed_resources import (
    CommittedResources,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    CommandDispatchError,
    InvalidLifecycleTransitionError,
    LifecycleTransitionConflictError,
    NoEligibleWorkerError,
    ServerNotFoundError,
    ServerNotRunningError,
)
from mc_server_dashboard_api.servers.domain.jar_provisioner import JarProvisioningError
from mc_server_dashboard_api.servers.domain.value_objects import (
    JAR_KEY_CONFIG_FIELD,
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from tests.servers.fakes import (
    FakeClock,
    FakeControlPlane,
    FakeJarProvisioner,
    FakeServerRepository,
    FakeStoreGenerationReader,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


class _OkFleetControlPlane(FleetControlPlane):
    """A fleet control plane that answers every dispatch OK (no Worker, no network).

    Lets the #778 placement-race tests drive the real
    :class:`FleetControlPlaneAdapter` (genuine reserve/confirm/release) while the
    hydrate/start dispatches succeed without a live Worker.
    """

    async def dispatch(
        self,
        *,
        worker_id: FleetWorkerId,
        server_id: str,
        command: FleetCommand,
        timeout_override: float | None = None,
    ) -> CommandResult:
        return CommandResult(code=CommandResultCode.OK)


def _registry_backed_control_plane(registry: object) -> FleetControlPlaneAdapter:
    return FleetControlPlaneAdapter(
        registry=registry,  # type: ignore[arg-type]
        control_plane=_OkFleetControlPlane(),
        data_plane_base_url="http://data-plane.test",
        worker_credential="test-credential",
    )


class _RacingServerRepository(FakeServerRepository):
    """Simulates a concurrent transition committing between load and the CAS.

    The use case loads the server (seeing the stale state that passes the
    in-memory transition check), then runs its compare-and-set. This double
    mutates the *stored* row right after the use case reads it, so the CAS sees
    the row already moved and matches no row (the lost-race signal).
    """

    def __init__(self, *, winner: Server) -> None:
        super().__init__()
        self._winner = winner

    async def get_by_id(self, server_id: ServerId) -> Server | None:
        loaded = await super().get_by_id(server_id)
        if loaded is not None:
            # A concurrent transition wins the race after our read.
            self.by_id[server_id] = self._winner
        return loaded


def _server(
    *,
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    desired: DesiredState = DesiredState.STOPPED,
    observed: ObservedState = ObservedState.STOPPED,
    worker_id: uuid.UUID | None = None,
    config: dict[str, object] | None = None,
) -> Server:
    return Server(
        id=ServerId(server_id),
        community_id=CommunityId(community_id),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.HOST_PROCESS,
        config={} if config is None else config,
        desired_state=desired,
        observed_state=observed,
        observed_at=None,
        assigned_worker_id=None if worker_id is None else WorkerId(worker_id),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _ids() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


# --- start -----------------------------------------------------------------


async def test_start_places_sets_running_and_dispatches() -> None:
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    cp = FakeControlPlane(place_to=WorkerId(worker))
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    result = await use_case(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    assert result.desired_state is DesiredState.RUNNING
    assert result.assigned_worker_id == WorkerId(worker)
    # Hydrate precedes the launch (FR-DATA-4), then StartServer dispatches.
    assert cp.dispatched == [
        ("hydrate", WorkerId(worker), ServerId(server_id)),
        ("start", WorkerId(worker), ServerId(server_id)),
    ]
    assert cp.incremented == [WorkerId(worker)]
    assert cp.decremented == []


async def test_start_forwards_request_memory_and_committed_accounting() -> None:
    # Resource-aware placement (#710): the use case sums the declared resources of
    # the servers already running on each Worker and forwards them, plus this
    # server's own memory request, through the control-plane seam.
    community, server_id, worker = _ids()
    other_worker = uuid.uuid4()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            config={"memory_limit_mb": 2048, "cpu_millis": 1000},
        )
    )
    # An unrelated server already running on `other_worker` contributes commitments.
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=uuid.uuid4(),
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=other_worker,
            config={"memory_limit_mb": 4096, "cpu_millis": 1500},
        )
    )
    cp = FakeControlPlane(place_to=WorkerId(worker))
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    await use_case(community_id=CommunityId(community), server_id=ServerId(server_id))

    assert cp.place_memory_limit_mb == 2048
    assert cp.place_committed_by_worker == {
        WorkerId(other_worker): CommittedResources(memory_mb=4096, cpu_millis=1500)
    }


async def test_start_records_resolved_jar_key_in_config() -> None:
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    cp = FakeControlPlane(place_to=WorkerId(worker))
    provisioner = FakeJarProvisioner(key="a" * 64)
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=provisioner,
        store_generation=FakeStoreGenerationReader(),
    )

    await use_case(community_id=CommunityId(community), server_id=ServerId(server_id))

    # The ensure ran (before placement), and the resolved key is persisted.
    assert provisioner.calls == [("vanilla", "1.21.1", None)]
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.config[JAR_KEY_CONFIG_FIELD] == "a" * 64


async def test_start_fails_before_placement_when_jar_provisioning_fails() -> None:
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    cp = FakeControlPlane(place_to=WorkerId(worker))
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(fail=True),
        store_generation=FakeStoreGenerationReader(),
    )

    with pytest.raises(JarProvisioningError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    # No placement, no dispatch, no desired-state flip: the start failed cleanly.
    assert cp.dispatched == []
    assert cp.incremented == []
    survivor = uow.servers.by_id[ServerId(server_id)]
    assert survivor.desired_state is DesiredState.STOPPED
    assert survivor.assigned_worker_id is None
    assert JAR_KEY_CONFIG_FIELD not in survivor.config


async def test_start_when_already_running_is_conflict() -> None:
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )
    use_case = StartServer(
        uow=uow,
        control_plane=FakeControlPlane(),
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    with pytest.raises(InvalidLifecycleTransitionError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )


async def test_start_with_no_eligible_worker_is_typed_error() -> None:
    community, server_id, _ = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    cp = FakeControlPlane(place_to=None)
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    with pytest.raises(NoEligibleWorkerError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )
    # Nothing was committed or dispatched.
    assert uow.servers.by_id[ServerId(server_id)].desired_state is DesiredState.STOPPED
    assert cp.dispatched == []


async def test_two_concurrent_starts_only_one_takes_the_last_slot() -> None:
    # The placement capacity race (#778): two starts of DIFFERENT servers run
    # concurrently against a worker with a single capacity slot. The reservation
    # taken at placement time means exactly one places and starts; the other sees no
    # eligible worker — the worker's max_servers is never oversubscribed. (Backed by
    # the real registry + control-plane adapter so the actual reservation runs.)
    import asyncio

    from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
    from tests.fleet.fakes import FakeClock as FleetFakeClock
    from tests.fleet.fakes import make_worker

    community = uuid.uuid4()
    server_a, server_b = uuid.uuid4(), uuid.uuid4()
    worker_uuid = uuid.uuid4()
    registry = InMemoryWorkerRegistry(
        clock=FleetFakeClock(_NOW), heartbeat_timeout=dt.timedelta(seconds=30)
    )
    registry.register(make_worker(worker_id=str(worker_uuid), max_servers=1, at=_NOW))
    cp = _registry_backed_control_plane(registry)

    # Yield between placement and commit so BOTH starts read the worker's load
    # (taking their reservation at _place) before EITHER commits and confirms.
    # Without this barrier none of the fakes suspends between _place and commit, so
    # asyncio.gather runs the two starts strictly sequentially and the second
    # already sees the first's committed load — passing even with pre-fix semantics
    # (no reservation). The reservation taken at _place time is what serializes the
    # last slot (#778); this yield is what exercises that, failing without the fix.
    class _YieldingUnitOfWork(FakeUnitOfWork):
        async def commit(self) -> None:
            await asyncio.sleep(0)
            await super().commit()

    async def start(server_id: uuid.UUID) -> object:
        uow = _YieldingUnitOfWork()
        uow.servers.seed(_server(community_id=community, server_id=server_id))
        use_case = StartServer(
            uow=uow,
            control_plane=cp,
            clock=FakeClock(_NOW),
            jar_provisioner=FakeJarProvisioner(),
            store_generation=FakeStoreGenerationReader(),
        )
        try:
            return await use_case(
                community_id=CommunityId(community), server_id=ServerId(server_id)
            )
        except NoEligibleWorkerError as exc:
            return exc

    results = await asyncio.gather(start(server_a), start(server_b))

    placed = [r for r in results if isinstance(r, Server)]
    rejected = [r for r in results if isinstance(r, NoEligibleWorkerError)]
    assert len(placed) == 1
    assert len(rejected) == 1
    # The committed load reflects exactly one placement: the worker is not
    # oversubscribed past its single slot.
    assert registry.list_workers()[0].assigned_count == 1


async def test_reregister_between_commit_and_increment_does_not_double_count() -> None:
    # The minor variant (#778): the worker re-registers (resetting its count) and is
    # rebuilt from the authoritative tally — which already includes this server's
    # just-committed row — in the window between the lifecycle commit and the
    # increment. The deferred increment must then be a no-op, not a second count.
    from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
    from tests.fleet.fakes import FakeClock as FleetFakeClock
    from tests.fleet.fakes import make_worker

    community, server_id, _ = _ids()
    worker_uuid = uuid.uuid4()
    registry = InMemoryWorkerRegistry(
        clock=FleetFakeClock(_NOW), heartbeat_timeout=dt.timedelta(seconds=30)
    )
    registry.register(make_worker(worker_id=str(worker_uuid), max_servers=4, at=_NOW))
    cp = _registry_backed_control_plane(registry)

    # A UnitOfWork whose commit simulates a Worker reconnect landing mid-commit: the
    # registry resets the count and rebuilds from the tally, which already includes
    # this committed server id.
    class _ReregisteringUnitOfWork(FakeUnitOfWork):
        async def commit(self) -> None:
            await super().commit()
            registry.register(make_worker(worker_id=str(worker_uuid), at=_NOW))
            epoch = registry.assignment_epoch(FleetWorkerId(str(worker_uuid)))
            registry.set_assignment(
                FleetWorkerId(str(worker_uuid)), {str(server_id): 0}, epoch
            )

    uow = _ReregisteringUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    await use_case(community_id=CommunityId(community), server_id=ServerId(server_id))

    # The rebuild counted the row once; the post-commit increment is a no-op, so the
    # load is exactly 1 (not 2).
    assert registry.list_workers()[0].assigned_count == 1


async def test_start_hydrate_failure_compensates_without_dispatching_start() -> None:
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    # The hydrate dispatch fails; the launch must never be attempted (FR-DATA-4:
    # the working set must be in place before the process starts).
    busy = CommandOutcome(status=CommandStatus.INVALID_STATE, message="busy")
    cp = FakeControlPlane(place_to=WorkerId(worker), outcomes={"hydrate": busy})
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    with pytest.raises(CommandDispatchError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    # Only hydrate was dispatched; start was never reached.
    assert [kind for kind, _, _ in cp.dispatched] == ["hydrate"]
    reverted = uow.servers.by_id[ServerId(server_id)]
    assert reverted.desired_state is DesiredState.STOPPED
    assert reverted.assigned_worker_id is None
    assert cp.incremented == [WorkerId(worker)]
    assert cp.decremented == [WorkerId(worker)]


async def test_start_failure_after_successful_hydrate_compensates() -> None:
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    # Hydrate succeeds, then the launch fails with a genuine refusal (e.g.
    # INTERNAL — NOT an INVALID_STATE, which means already-running and is treated
    # as convergence): both dispatches ran, and the committed intent must still be
    # compensated.
    busy = CommandOutcome(status=CommandStatus.INTERNAL, message="boom")
    cp = FakeControlPlane(place_to=WorkerId(worker), outcomes={"start": busy})
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    with pytest.raises(CommandDispatchError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    assert [kind for kind, _, _ in cp.dispatched] == ["hydrate", "start"]
    reverted = uow.servers.by_id[ServerId(server_id)]
    assert reverted.desired_state is DesiredState.STOPPED
    assert reverted.assigned_worker_id is None
    assert cp.incremented == [WorkerId(worker)]
    assert cp.decremented == [WorkerId(worker)]


async def test_start_lost_response_after_dispatch_keeps_assignment() -> None:
    # The lost-response case for the normal start (#773, mirroring #101's fix to
    # place_and_start): hydrate succeeds, the start command is SENT, then its
    # response is lost (WorkerUnavailableError, i.e. a CommandTimedOut / stream
    # death). The Worker MAY have applied it, so __call__ must NOT compensate —
    # keep desired=running and the assignment so the reconciler redispatches to
    # the SAME Worker (an INVALID_STATE there resolves it as already-running).
    # Compensating here would orphan a live instance and let a later start place
    # a second one on a different Worker.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    cp = FakeControlPlane(place_to=WorkerId(worker), unavailable_kinds={"start"})
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    with pytest.raises(WorkerUnavailableError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    # The start command was attempted (sent) before the response was lost.
    assert [kind for kind, _, _ in cp.dispatched] == ["hydrate", "start"]
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.desired_state is DesiredState.RUNNING
    assert stored.assigned_worker_id == WorkerId(worker)
    # No compensation: the load increment stands, nothing is decremented.
    assert cp.incremented == [WorkerId(worker)]
    assert cp.decremented == []


async def test_start_pre_dispatch_unavailable_compensates() -> None:
    # A WorkerUnavailableError BEFORE the start was sent (here a failed hydrate)
    # means the start never reached the Worker, so __call__ must compensate the
    # committed intent it created: revert desired->stopped, unassign, and
    # decrement the load (#773). Mirrors the pre-dispatch leg of place_and_start.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    cp = FakeControlPlane(place_to=WorkerId(worker), unavailable_kinds={"hydrate"})
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    with pytest.raises(WorkerUnavailableError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    # The start was never sent — only the hydrate was attempted.
    assert [kind for kind, _, _ in cp.dispatched] == ["hydrate"]
    reverted = uow.servers.by_id[ServerId(server_id)]
    assert reverted.desired_state is DesiredState.STOPPED
    assert reverted.assigned_worker_id is None
    assert cp.incremented == [WorkerId(worker)]
    assert cp.decremented == [WorkerId(worker)]


async def test_start_invalid_state_outcome_keeps_assignment_as_running() -> None:
    # INVALID_STATE returned to __call__ is not a "nothing started" refusal: the
    # Worker rejects a start only when an instance for this server is ALREADY live
    # on the assigned Worker (already running, or a pending failed-stop orphan;
    # instancemanager handleStart). Compensating (desired->stopped + unassign +
    # decrement) would orphan that live instance and let a later start place a
    # SECOND one on a different Worker (#773/#774). So __call__ must converge like
    # redispatch_start (#213): keep desired=running + the assignment, record
    # observed=running, and return success — no compensation, no decrement.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    cp = FakeControlPlane(
        place_to=WorkerId(worker),
        outcomes={
            "start": CommandOutcome(
                status=CommandStatus.INVALID_STATE, message="already running"
            )
        },
    )
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    result = await use_case(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    # Success returned, with observed=running reflected on the entity.
    assert result.desired_state is DesiredState.RUNNING
    assert result.observed_state is ObservedState.RUNNING
    assert result.observed_at == _NOW
    # The start was sent; no compensation followed.
    assert [kind for kind, _, _ in cp.dispatched] == ["hydrate", "start"]
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.desired_state is DesiredState.RUNNING
    assert stored.assigned_worker_id == WorkerId(worker)
    assert stored.observed_state is ObservedState.RUNNING
    assert stored.observed_at == _NOW
    # No accounting drift: the load increment stands, nothing is decremented.
    assert cp.incremented == [WorkerId(worker)]
    assert cp.decremented == []


async def test_start_busy_outcome_keeps_assignment_without_converging() -> None:
    # BUSY returned to __call__ post-dispatch (issue #824): another lifecycle
    # command for this id is already in flight on the Worker and its outcome is
    # UNKNOWN -- distinct from INVALID_STATE (already running). So __call__ must
    # NOT converge observed=running (the raced original may still FAIL and leave the
    # server down), and must NOT compensate (a same-Worker redispatch is required to
    # honor the start once the in-flight command settles). It raises a retryable
    # conflict while KEEPING desired=running + the assignment, with no decrement.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    cp = FakeControlPlane(
        place_to=WorkerId(worker),
        outcomes={
            "start": CommandOutcome(status=CommandStatus.BUSY, message="in flight")
        },
    )
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    with pytest.raises(CommandDispatchError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    # The start was sent (post-dispatch BUSY), and NO compensation followed.
    assert [kind for kind, _, _ in cp.dispatched] == ["hydrate", "start"]
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.desired_state is DesiredState.RUNNING
    assert stored.assigned_worker_id == WorkerId(worker)
    # No speculative convergence: observed is NOT recorded as running.
    assert stored.observed_state is not ObservedState.RUNNING
    # The load increment stands; nothing is decremented (assignment kept for retry).
    assert cp.incremented == [WorkerId(worker)]
    assert cp.decremented == []


async def test_start_pre_dispatch_busy_compensates() -> None:
    # A BUSY on the HYDRATE (the same reservation race, before the start is sent):
    # the start never reached the Worker, so __call__ must compensate normally
    # (desired->stopped + unassign + decrement), exactly as a pre-dispatch
    # INVALID_STATE does. The post-dispatch keep-assignment arm is gated on
    # ``dispatch.attempted`` so this pre-dispatch case falls through to compensation.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    cp = FakeControlPlane(
        place_to=WorkerId(worker),
        outcomes={
            "hydrate": CommandOutcome(status=CommandStatus.BUSY, message="in flight")
        },
    )
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    with pytest.raises(CommandDispatchError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    # Only hydrate was dispatched; start was never reached, so the intent reverts.
    assert [kind for kind, _, _ in cp.dispatched] == ["hydrate"]
    reverted = uow.servers.by_id[ServerId(server_id)]
    assert reverted.desired_state is DesiredState.STOPPED
    assert reverted.assigned_worker_id is None
    assert cp.incremented == [WorkerId(worker)]
    assert cp.decremented == [WorkerId(worker)]


async def test_start_failure_logs_warning_with_server_and_kind(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A failed start dispatch turns into a CommandDispatchError; the Worker's
    # message is logged at WARN with server_id and command kind context so a
    # failure is diagnosable, while the raw message stays out of the HTTP body
    # (issue #194).
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    busy = CommandOutcome(status=CommandStatus.INTERNAL, message="instance busy")
    cp = FakeControlPlane(place_to=WorkerId(worker), outcomes={"start": busy})
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(CommandDispatchError),
    ):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    record = next(r for r in caplog.records if r.levelno == logging.WARNING)
    message = record.getMessage()
    assert "instance busy" in message
    assert "StartServer" in message
    assert str(server_id) in message


async def test_stop_failure_logs_warning_with_server_and_kind(
    caplog: pytest.LogCaptureFixture,
) -> None:
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )
    busy = CommandOutcome(status=CommandStatus.INTERNAL, message="stop refused")
    cp = FakeControlPlane(outcomes={"stop": busy})
    use_case = StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(CommandDispatchError),
    ):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    record = next(
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "stop refused" in r.getMessage()
    )
    message = record.getMessage()
    assert "StopServer" in message
    assert str(server_id) in message


async def test_start_compensation_decrements_only_when_revert_applies() -> None:
    # Dispatch fails, so the committed start intent is compensated. The revert
    # compare-and-set matches the still-running row, so the placement-load
    # decrement runs exactly once, symmetric with the increment.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    busy = CommandOutcome(status=CommandStatus.INTERNAL, message="boom")
    cp = FakeControlPlane(place_to=WorkerId(worker), outcomes={"start": busy})
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    with pytest.raises(CommandDispatchError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    assert cp.incremented == [WorkerId(worker)]
    assert cp.decremented == [WorkerId(worker)]


async def test_start_compensation_skips_decrement_when_revert_loses_race() -> None:
    # Dispatch fails, so the committed start intent is compensated. But a
    # concurrent stop already reverted the row to stopped before compensation
    # ran, so the revert compare-and-set matches no row. That concurrent stop
    # owns the placement-load decrement; compensation must NOT decrement again,
    # or the count would fall below the true running tally.
    community, server_id, worker = _ids()

    class _StopRacesCompensation(FakeServerRepository):
        """A concurrent stop reverts the row right before compensation reads it.

        The start commits running+assigned; on the compensation's read the row
        is mutated to stopped/unassigned, so the revert CAS (expected_from=
        running) matches no row — the lost-race signal for compensation.
        """

        def __init__(self) -> None:
            super().__init__()
            self._reads = 0

        async def get_by_id(self, server_id: ServerId) -> Server | None:
            loaded = await super().get_by_id(server_id)
            self._reads += 1
            # Reads: 1 = start's load, 2 = compensation's load. On the
            # compensation read, simulate the concurrent stop having already
            # reverted the stored row.
            if self._reads == 2:
                stored = self.by_id[server_id]
                stored.desired_state = DesiredState.STOPPED
                stored.assigned_worker_id = None
            return loaded

    uow = FakeUnitOfWork(servers=_StopRacesCompensation())
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    busy = CommandOutcome(status=CommandStatus.INTERNAL, message="boom")
    cp = FakeControlPlane(place_to=WorkerId(worker), outcomes={"start": busy})
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    with pytest.raises(CommandDispatchError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    assert cp.incremented == [WorkerId(worker)]
    assert cp.decremented == []


async def test_start_lost_race_is_conflict_without_dispatch_or_count() -> None:
    # The in-memory check passes (loaded as stopped/unassigned), but a concurrent
    # start commits running+assigned before our compare-and-set runs. The CAS
    # matches no row, so we must 409 without dispatching or touching counts.
    community, server_id, winner_worker = _ids()
    placed_worker = uuid.uuid4()
    winner = _server(
        community_id=community,
        server_id=server_id,
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
        worker_id=winner_worker,
    )
    uow = FakeUnitOfWork(servers=_RacingServerRepository(winner=winner))
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    cp = FakeControlPlane(place_to=WorkerId(placed_worker))
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    with pytest.raises(LifecycleTransitionConflictError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    # No dispatch, no commit, no committed count change: the winner's row is
    # untouched. The placement reservation taken before the lost CAS is released so
    # the tentatively-held slot is freed (#778).
    assert cp.dispatched == []
    assert cp.incremented == []
    assert cp.decremented == []
    assert cp.released == [(WorkerId(placed_worker), ServerId(server_id))]
    assert uow.commits == 0
    survivor = uow.servers.by_id[ServerId(server_id)]
    assert survivor.assigned_worker_id == WorkerId(winner_worker)


async def test_start_release_reservation_when_commit_raises() -> None:
    # The CAS applied but the commit itself raises (e.g. a DB error). The reservation
    # taken at placement must be released so the tentatively-held slot is not leaked
    # permanently (#778): a never-committed server id never enters the authoritative
    # tally, so no reconnect rebuild would ever reclaim it. (If the commit actually
    # landed, releasing only undercounts by one until the next rebuild — safe.)
    from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
    from tests.fleet.fakes import FakeClock as FleetFakeClock
    from tests.fleet.fakes import make_worker

    community, server_id, _ = _ids()
    worker_uuid = uuid.uuid4()
    registry = InMemoryWorkerRegistry(
        clock=FleetFakeClock(_NOW), heartbeat_timeout=dt.timedelta(seconds=30)
    )
    registry.register(make_worker(worker_id=str(worker_uuid), max_servers=1, at=_NOW))
    cp = _registry_backed_control_plane(registry)

    class _FailingCommitUnitOfWork(FakeUnitOfWork):
        async def commit(self) -> None:
            raise RuntimeError("commit failed")

    uow = _FailingCommitUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    # The reservation was released, so the worker's capacity is NOT shrunk: its load
    # is back to 0 and it can accept a placement again (no API restart needed).
    assert registry.list_workers()[0].assigned_count == 0


async def test_start_release_reservation_when_cancelled() -> None:
    # The request task is cancelled at the commit await (a client disconnect cancels
    # the HTTP task). CancelledError is a BaseException, not Exception, so the leak
    # fix must catch it explicitly to release the reservation before re-raising (#778).
    from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
    from tests.fleet.fakes import FakeClock as FleetFakeClock
    from tests.fleet.fakes import make_worker

    community, server_id, _ = _ids()
    worker_uuid = uuid.uuid4()
    registry = InMemoryWorkerRegistry(
        clock=FleetFakeClock(_NOW), heartbeat_timeout=dt.timedelta(seconds=30)
    )
    registry.register(make_worker(worker_id=str(worker_uuid), max_servers=1, at=_NOW))
    cp = _registry_backed_control_plane(registry)

    class _CancelOnCommitUnitOfWork(FakeUnitOfWork):
        async def commit(self) -> None:
            raise asyncio.CancelledError()

    uow = _CancelOnCommitUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    with pytest.raises(asyncio.CancelledError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    # The reservation was released despite the cancellation: capacity is not shrunk.
    assert registry.list_workers()[0].assigned_count == 0


async def test_start_confirms_reservation_when_cancelled_after_commit() -> None:
    # The commit lands, then the request task is cancelled at the UoW ``__aexit__``
    # await (its rollback()/close() are real suspension points outside the
    # CAS+commit try/except). The confirm now runs INSIDE the transaction, right
    # after the commit returns, so the reservation is already promoted to a
    # committed assignment before the cancellation unwinds: the count stays truthful
    # (1) and is not leaked as a pending reservation that no rebuild would reclaim
    # (#840). It must NOT be released either — the committed row is real.
    from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
    from tests.fleet.fakes import FakeClock as FleetFakeClock
    from tests.fleet.fakes import make_worker

    community, server_id, _ = _ids()
    worker_uuid = uuid.uuid4()
    registry = InMemoryWorkerRegistry(
        clock=FleetFakeClock(_NOW), heartbeat_timeout=dt.timedelta(seconds=30)
    )
    registry.register(make_worker(worker_id=str(worker_uuid), max_servers=1, at=_NOW))
    cp = _registry_backed_control_plane(registry)

    class _CancelOnExitUnitOfWork(FakeUnitOfWork):
        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            # Cancellation delivered at the teardown await, after a successful commit.
            raise asyncio.CancelledError()

    uow = _CancelOnExitUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    with pytest.raises(asyncio.CancelledError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    # Confirmed, not leaked: the reservation was promoted to a committed assignment
    # (count == 1), and the reservation bucket is empty so no later set_assignment
    # rebuild would have to reclaim a stale pending entry.
    assert uow.commits == 1
    assert registry.list_workers()[0].assigned_count == 1
    assert registry.reserved_memory_mb(FleetWorkerId(str(worker_uuid))) == 0
    # An authoritative rebuild that omits this server (it has since stopped) drops the
    # count to 0 — proving the entry was a committed assignment, not a stuck
    # reservation that set_assignment can never clear. The snapshot epoch is read here
    # (after the confirm), so the confirm is not preserved and the empty tally wins.
    epoch = registry.assignment_epoch(FleetWorkerId(str(worker_uuid)))
    registry.set_assignment(FleetWorkerId(str(worker_uuid)), {}, epoch)
    assert registry.list_workers()[0].assigned_count == 0


async def test_two_sequential_starts_second_is_conflict() -> None:
    # The first start wins; a second start against the now-running row loses the
    # in-memory check (already running) and is a conflict — only one placement.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    cp = FakeControlPlane(place_to=WorkerId(worker))
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    await use_case(community_id=CommunityId(community), server_id=ServerId(server_id))
    with pytest.raises(InvalidLifecycleTransitionError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    # Exactly one placement/dispatch happened across both attempts (the lone
    # successful start hydrates then launches).
    assert cp.dispatched == [
        ("hydrate", WorkerId(worker), ServerId(server_id)),
        ("start", WorkerId(worker), ServerId(server_id)),
    ]
    assert cp.incremented == [WorkerId(worker)]


async def test_start_compensation_failure_preserves_both_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Dispatch fails, then the compensation commit also fails. The compensation
    # error must propagate chained from the original dispatch failure so neither
    # is masked, both errors are logged, and the placement-load decrement is
    # skipped: the DB state is unknown, so a reconnect rebuild reconciles.
    community, server_id, worker = _ids()

    class _FailOnSecondCommit(FakeUnitOfWork):
        async def commit(self) -> None:
            self.commits += 1
            if self.commits == 2:  # the compensation commit
                raise RuntimeError("compensation commit failed")

    uow = _FailOnSecondCommit()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    cp = FakeControlPlane(
        place_to=WorkerId(worker),
        outcome=CommandOutcome(status=CommandStatus.INVALID_STATE, message="busy"),
    )
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(RuntimeError, match="compensation commit failed") as excinfo,
    ):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    # The original dispatch failure is preserved as the chained cause.
    assert isinstance(excinfo.value.__cause__, CommandDispatchError)
    # DB state is unknown after the failed commit: do not decrement.
    assert cp.decremented == []
    # Both the compensation error and the original dispatch failure are logged.
    record = next(r for r in caplog.records if r.levelno == logging.ERROR)
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], RuntimeError)
    assert "busy" in record.getMessage()


async def test_start_missing_server_is_not_found() -> None:
    community, server_id, _ = _ids()
    use_case = StartServer(
        uow=FakeUnitOfWork(),
        control_plane=FakeControlPlane(),
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )
    with pytest.raises(ServerNotFoundError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )


async def test_start_cross_community_is_not_found() -> None:
    community, other, server_id = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=other, server_id=server_id))
    use_case = StartServer(
        uow=uow,
        control_plane=FakeControlPlane(),
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )
    with pytest.raises(ServerNotFoundError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )


# --- stop ------------------------------------------------------------------


async def test_stop_sets_stopped_dispatches_and_decrements() -> None:
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )
    cp = FakeControlPlane()
    use_case = StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

    result = await use_case(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    assert result.desired_state is DesiredState.STOPPED
    # A graceful stop quiesces the process, then takes a final snapshot
    # (FR-DATA-4, FR-DATA-7).
    assert cp.dispatched == [
        ("stop", WorkerId(worker), ServerId(server_id)),
        ("snapshot", WorkerId(worker), ServerId(server_id)),
    ]
    assert cp.decremented == [WorkerId(worker)]
    # Default is graceful: the stop dispatch carries force=False (issue #270).
    assert cp.stop_force is False


async def test_stop_force_forwards_force_to_control_plane() -> None:
    # An explicit force stop threads force=True down to the control-plane dispatch
    # (worker side then takes the immediate-kill path); issue #270.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )
    cp = FakeControlPlane()
    use_case = StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

    await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        force=True,
    )

    assert cp.stop_force is True


async def test_stop_graceful_success_unassigns_and_records_stopped() -> None:
    # A graceful stop confirms the process is gone, so the assignment is cleared
    # and observed converges to stopped in the same transaction (issue #206).
    # Clearing the assignment lets a later start re-place (require_unassigned).
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )
    cp = FakeControlPlane()
    use_case = StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

    result = await use_case(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    assert result.assigned_worker_id is None
    assert result.observed_state is ObservedState.STOPPED
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.assigned_worker_id is None
    assert stored.observed_state is ObservedState.STOPPED
    # The placement-load decrement still happens exactly once, on the desired
    # flip -- the later unassign must not double-decrement (issue #206).
    assert cp.decremented == [WorkerId(worker)]


async def test_stop_then_start_succeeds() -> None:
    # The regression that escaped (issue #206): stop must unassign so the
    # subsequent start's require_unassigned compare-and-set can re-place the
    # server instead of 409ing forever.
    community, server_id, worker = _ids()
    next_worker = uuid.uuid4()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )
    await StopServer(uow=uow, control_plane=FakeControlPlane(), clock=FakeClock(_NOW))(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    started = await StartServer(
        uow=uow,
        control_plane=FakeControlPlane(place_to=WorkerId(next_worker)),
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )(community_id=CommunityId(community), server_id=ServerId(server_id))

    assert started.desired_state is DesiredState.RUNNING
    assert started.assigned_worker_id == WorkerId(next_worker)


async def test_stop_holds_assignment_until_final_snapshot_settles() -> None:
    # Issue #847: the unassign must be DELAYED until the final snapshot returns. A
    # start that races the in-flight final snapshot would otherwise re-place the
    # server on a DIFFERENT worker, whose hydrate pulls store generation N while the
    # final (would-be N+1) is still uploading -- the final progression goes missing
    # from the booted world. Holding the assignment across the snapshot keeps the
    # require_unassigned compare-and-set failing, so no cross-worker re-placement can
    # slip in during the snapshot window.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )

    class _AssertsHeldDuringSnapshot(FakeControlPlane):
        async def snapshot(
            self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
        ) -> CommandOutcome:
            # At snapshot time the row is observed=stopped but STILL assigned, so a
            # racing start's require_unassigned CAS cannot re-place it.
            row = uow.servers.by_id[server_id]
            self.assignment_at_snapshot = row.assigned_worker_id
            self.observed_at_snapshot = row.observed_state
            return await super().snapshot(
                worker_id=worker_id, community_id=community_id, server_id=server_id
            )

    cp = _AssertsHeldDuringSnapshot()
    result = await StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    assert cp.observed_at_snapshot is ObservedState.STOPPED
    assert cp.assignment_at_snapshot == WorkerId(worker)
    # Once the snapshot settled, the assignment is cleared so a later start can
    # re-place under require_unassigned.
    assert result.assigned_worker_id is None
    assert uow.servers.by_id[ServerId(server_id)].assigned_worker_id is None
    assert [kind for kind, _, _ in cp.dispatched] == ["stop", "snapshot"]


async def test_stop_start_race_cannot_replace_until_final_snapshot_settles() -> None:
    # Issue #847: a start issued WHILE the final snapshot is in flight must not be
    # able to re-place the server elsewhere -- the held assignment makes its
    # require_unassigned CAS conflict. The start is only admitted once the snapshot
    # has settled and the unassign has landed.
    community, server_id, worker = _ids()
    next_worker = uuid.uuid4()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )

    class _StartsDuringSnapshot(FakeControlPlane):
        async def snapshot(
            self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
        ) -> CommandOutcome:
            # A user start lands mid-snapshot. It must conflict (assignment still
            # held), NOT re-place on next_worker.
            with pytest.raises(LifecycleTransitionConflictError):
                await StartServer(
                    uow=uow,
                    control_plane=FakeControlPlane(place_to=WorkerId(next_worker)),
                    clock=FakeClock(_NOW),
                    jar_provisioner=FakeJarProvisioner(),
                    store_generation=FakeStoreGenerationReader(),
                )(community_id=community_id, server_id=server_id)
            return await super().snapshot(
                worker_id=worker_id, community_id=community_id, server_id=server_id
            )

    cp = _StartsDuringSnapshot()
    await StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    # The mid-snapshot start was rejected; the row is stopped+unassigned afterward.
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.desired_state is DesiredState.STOPPED
    assert stored.assigned_worker_id is None


async def test_stop_cancelled_mid_snapshot_holds_assignment() -> None:
    # Issue #847 (bug 2, round-3): if the HTTP request task is cancelled WHILE the
    # final snapshot is in flight (a client disconnect cancels the task at the
    # snapshot await), the dispatched snapshot keeps uploading worker-side — the
    # proto has no command-cancel and abandoning the pending future signals nothing.
    # So the assignment must be HELD (NOT released): clearing it would free the row
    # while the upload is live, letting a racing start re-place on a different worker
    # and reopening the stop->re-place race. The row stays at (stopped, stopped,
    # assigned); the reconciler's stale-stop arm recovers it once grace lapses (by
    # which point the upload has settled, grace > snapshot budget).
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )

    class _SnapshotCancelled(FakeControlPlane):
        async def snapshot(
            self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
        ) -> CommandOutcome:
            self.dispatched.append(("snapshot", worker_id, server_id))
            # The client disconnects: the request task is cancelled at this await.
            raise asyncio.CancelledError

    cp = _SnapshotCancelled()
    with pytest.raises(asyncio.CancelledError):
        await StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    # The CancelledError propagated and the assignment was deliberately HELD — the
    # upload is still live, so the stale-stop reconciler arm (not an immediate clear)
    # owns the grace-bounded recovery.
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.desired_state is DesiredState.STOPPED
    assert stored.observed_state is ObservedState.STOPPED
    assert stored.assigned_worker_id == WorkerId(worker)


async def test_stop_final_snapshot_timeout_holds_assignment() -> None:
    # Issue #847 (round-3): a final-snapshot dispatch TIMEOUT must HOLD the
    # assignment, mirroring the cancel-hold path. A timeout means the worker session
    # is healthy and the upload is still in flight (the proto has no command-cancel;
    # the API deadline only abandoned the pending future), so clearing here would
    # release the row while the upload is live, letting a racing start re-place on a
    # different worker and reopening the stop->re-place race. The adapter encodes the
    # cause as ``upload_may_be_live=True`` (set from a ``CommandTimedOutError`` cause);
    # the row stays at (stopped, stopped, assigned) for the stale-stop arm to recover.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )

    class _SnapshotTimesOut(FakeControlPlane):
        async def snapshot(
            self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
        ) -> CommandOutcome:
            self.dispatched.append(("snapshot", worker_id, server_id))
            # Mirror the adapter's wrap of a CommandTimedOutError: a
            # WorkerUnavailableError flagged as upload-may-be-live, cause chained.
            try:
                raise CommandTimedOutError(str(worker_id.value))
            except CommandTimedOutError as exc:
                raise WorkerUnavailableError(
                    str(worker_id.value), upload_may_be_live=True
                ) from exc

    cp = _SnapshotTimesOut()
    result = await StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    # The timeout is caught inside _final_snapshot (the stop succeeded), but the
    # assignment is deliberately HELD — the upload may still be live, so the
    # stale-stop arm (not an immediate clear) owns the grace-bounded recovery.
    assert result.observed_state is ObservedState.STOPPED
    assert result.assigned_worker_id == WorkerId(worker)
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.desired_state is DesiredState.STOPPED
    assert stored.observed_state is ObservedState.STOPPED
    assert stored.assigned_worker_id == WorkerId(worker)


async def test_stop_final_snapshot_disconnect_clears_assignment() -> None:
    # Issue #847 (round-3): the OTHER half of the WorkerUnavailableError split. A
    # worker DISCONNECT (``WorkerNotConnectedError`` cause, ``upload_may_be_live``
    # False) means the worker session is gone and the upload died with its ctx —
    # nothing is uploading, so the assignment is cleared immediately as before, so a
    # later same-worker start reuses the retained scratch (#845).
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )

    class _SnapshotDisconnects(FakeControlPlane):
        async def snapshot(
            self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
        ) -> CommandOutcome:
            self.dispatched.append(("snapshot", worker_id, server_id))
            try:
                raise WorkerNotConnectedError(str(worker_id.value))
            except WorkerNotConnectedError as exc:
                # The adapter leaves upload_may_be_live at its False default here.
                raise WorkerUnavailableError(str(worker_id.value)) from exc

    cp = _SnapshotDisconnects()
    result = await StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    assert result.observed_state is ObservedState.STOPPED
    assert result.assigned_worker_id is None
    assert uow.servers.by_id[ServerId(server_id)].assigned_worker_id is None


async def test_stop_unassigns_even_when_final_snapshot_fails() -> None:
    # Issue #847 / #845: on a FAILED final snapshot the assignment is still cleared
    # so the next same-worker start reuses the retained scratch (#845). Cross-worker
    # placement of a failed-final server keeps the #845-documented exposure.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )

    class _SnapshotFails(FakeControlPlane):
        async def snapshot(
            self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
        ) -> CommandOutcome:
            self.dispatched.append(("snapshot", worker_id, server_id))
            return CommandOutcome(
                status=CommandStatus.TRANSFER_FAILED, message="empty_snapshot"
            )

    cp = _SnapshotFails()
    result = await StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    assert result.observed_state is ObservedState.STOPPED
    assert result.assigned_worker_id is None
    assert uow.servers.by_id[ServerId(server_id)].assigned_worker_id is None


async def test_stop_failed_dispatch_keeps_assignment() -> None:
    # A failed stop dispatch may have left the process alive: the assignment must
    # stick so the reconciler's redispatch_stop owns convergence (issue #206).
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )
    cp = FakeControlPlane(
        outcomes={"stop": CommandOutcome(status=CommandStatus.INTERNAL, message="boom")}
    )
    use_case = StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

    with pytest.raises(CommandDispatchError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.assigned_worker_id == WorkerId(worker)


async def test_stop_succeeds_even_when_final_snapshot_fails() -> None:
    # A failing final snapshot must not fail the stop itself: the server is down
    # and the stop already succeeded; the snapshot is best-effort (FR-DATA-7).
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )

    class _SnapshotFails(FakeControlPlane):
        async def snapshot(
            self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
        ) -> CommandOutcome:
            self.dispatched.append(("snapshot", worker_id, server_id))
            return CommandOutcome(status=CommandStatus.TRANSFER_FAILED, message="boom")

    cp = _SnapshotFails()
    use_case = StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

    result = await use_case(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    assert result.desired_state is DesiredState.STOPPED
    assert ("stop", WorkerId(worker), ServerId(server_id)) in cp.dispatched
    assert ("snapshot", WorkerId(worker), ServerId(server_id)) in cp.dispatched


async def test_stop_final_snapshot_failure_logs_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The final-stop snapshot is the ONLY final-snapshot path and the server is now
    # stopped+unassigned, so a failure here is unrecoverable data loss — it must be
    # logged LOUD (error level), not swallowed as a warning (issue #841). A silent
    # warning is what hid the regression where the worker packed an empty snapshot.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )

    class _SnapshotFails(FakeControlPlane):
        async def snapshot(
            self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
        ) -> CommandOutcome:
            self.dispatched.append(("snapshot", worker_id, server_id))
            return CommandOutcome(
                status=CommandStatus.TRANSFER_FAILED, message="empty_snapshot"
            )

    use_case = StopServer(
        uow=uow, control_plane=_SnapshotFails(), clock=FakeClock(_NOW)
    )

    with caplog.at_level(logging.ERROR):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    record = next(
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "final snapshot" in r.getMessage()
    )
    assert str(server_id) in record.getMessage()


async def test_stop_when_already_stopped_is_conflict() -> None:
    community, server_id, _ = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    use_case = StopServer(
        uow=uow, control_plane=FakeControlPlane(), clock=FakeClock(_NOW)
    )
    with pytest.raises(InvalidLifecycleTransitionError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )


async def test_stop_lost_race_is_conflict_without_dispatch_or_count() -> None:
    # Loaded as running+assigned (in-memory check passes), but a concurrent stop
    # commits stopped before our compare-and-set. The CAS matches no row, so we
    # 409 without dispatching or decrementing the placement load.
    community, server_id, worker = _ids()
    winner = _server(
        community_id=community,
        server_id=server_id,
        desired=DesiredState.STOPPED,
        observed=ObservedState.STOPPED,
        worker_id=worker,
    )
    uow = FakeUnitOfWork(servers=_RacingServerRepository(winner=winner))
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )
    cp = FakeControlPlane()
    use_case = StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

    with pytest.raises(LifecycleTransitionConflictError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    assert cp.dispatched == []
    assert cp.decremented == []
    assert uow.commits == 0


async def test_stop_server_not_found_converges_to_stopped() -> None:
    # Stopping a server the worker no longer runs (e.g. crashed on the EULA,
    # issue #197): the worker holds no live instance and its handleStop answers
    # SERVER_NOT_FOUND -- not INVALID_STATE
    # (worker/internal/application/instancemanager/instancemanager.go:308-312).
    # That is a no-op stop, not a failure -> converge observed to stopped and
    # report success rather than surfacing command_failed.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.CRASHED,
            worker_id=worker,
        )
    )
    cp = FakeControlPlane(
        outcomes={"stop": CommandOutcome(status=CommandStatus.SERVER_NOT_FOUND)}
    )
    use_case = StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

    result = await use_case(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    assert result.desired_state is DesiredState.STOPPED
    assert result.observed_state is ObservedState.STOPPED
    # No live instance remains, so the assignment is cleared too (issue #206),
    # letting a later start re-place under require_unassigned.
    assert result.assigned_worker_id is None
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.desired_state is DesiredState.STOPPED
    assert stored.observed_state is ObservedState.STOPPED
    assert stored.assigned_worker_id is None
    # No live instance to snapshot: the stop dispatched, the snapshot did not.
    assert [kind for kind, _, _ in cp.dispatched] == ["stop"]
    assert cp.decremented == [WorkerId(worker)]


async def test_stop_server_not_found_returned_entity_honest_when_write_dropped() -> (
    None
):
    # Honesty fix (issue #292): under a same-instant clock the #216 monotonic guard
    # drops the convergence write (a fresher StatusChange already stamped the row at
    # the same instant). The returned entity must reflect the DROPPED write -- it
    # must not optimistically claim observed=stopped / unassigned that did not land.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    seeded = _server(
        community_id=community,
        server_id=server_id,
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
        worker_id=worker,
    )
    # A fresher StatusChange already landed at the clock's instant, so the guard
    # (observed_at <= current) drops the convergence write stamped at the same _NOW.
    seeded.observed_at = _NOW
    uow.servers.seed(seeded)
    cp = FakeControlPlane(
        outcomes={"stop": CommandOutcome(status=CommandStatus.SERVER_NOT_FOUND)}
    )

    result = await StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    stored = uow.servers.by_id[ServerId(server_id)]
    # The guard dropped the write, so the row keeps its observed cache and assignment.
    assert stored.observed_state is ObservedState.RUNNING
    assert stored.assigned_worker_id == WorkerId(worker)
    # The returned entity must agree with the row, not the optimistic mutation.
    assert result.observed_state is ObservedState.RUNNING
    assert result.assigned_worker_id == WorkerId(worker)


async def test_stop_server_not_found_then_start_succeeds() -> None:
    # The SERVER_NOT_FOUND convergence path must also unassign, so the stop ->
    # start chain succeeds after a crashed/already-gone instance (issue #206).
    community, server_id, worker = _ids()
    next_worker = uuid.uuid4()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.CRASHED,
            worker_id=worker,
        )
    )
    await StopServer(
        uow=uow,
        control_plane=FakeControlPlane(
            outcomes={"stop": CommandOutcome(status=CommandStatus.SERVER_NOT_FOUND)}
        ),
        clock=FakeClock(_NOW),
    )(community_id=CommunityId(community), server_id=ServerId(server_id))

    started = await StartServer(
        uow=uow,
        control_plane=FakeControlPlane(place_to=WorkerId(next_worker)),
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )(community_id=CommunityId(community), server_id=ServerId(server_id))

    assert started.desired_state is DesiredState.RUNNING
    assert started.assigned_worker_id == WorkerId(next_worker)


@pytest.mark.parametrize(
    "status",
    [
        # A genuine dispatch failure must raise.
        CommandStatus.INTERNAL,
        # INVALID_STATE is NOT a stop-convergence trigger: the Worker's handleStop
        # never emits it (only handleHydrate and handleStart do), so on stop it must
        # fail loudly rather than converge silently
        # (worker/internal/application/instancemanager/instancemanager.go:308-312).
        CommandStatus.INVALID_STATE,
    ],
)
async def test_stop_other_failure_still_surfaces_command_error(
    status: CommandStatus,
) -> None:
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )
    cp = FakeControlPlane(
        outcomes={"stop": CommandOutcome(status=status, message="boom")}
    )
    use_case = StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

    with pytest.raises(CommandDispatchError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )


# --- restart ---------------------------------------------------------------


async def test_restart_dispatches_and_keeps_running() -> None:
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )
    cp = FakeControlPlane()
    use_case = RestartServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

    result = await use_case(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    assert result.desired_state is DesiredState.RUNNING
    assert cp.dispatched == [("restart", WorkerId(worker), ServerId(server_id))]


async def test_restart_lost_race_is_conflict_without_dispatch() -> None:
    # The in-memory check passes (loaded as running/assigned), but a concurrent
    # stop commits stopped before our compare-and-set runs. The CAS
    # (expected_from=running) matches no row, so we must 409 without dispatching
    # the restart (FR-SRV-2).
    community, server_id, worker = _ids()
    stopped = _server(
        community_id=community,
        server_id=server_id,
        desired=DesiredState.STOPPED,
        observed=ObservedState.STOPPED,
    )
    uow = FakeUnitOfWork(servers=_RacingServerRepository(winner=stopped))
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )
    cp = FakeControlPlane()
    use_case = RestartServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

    with pytest.raises(LifecycleTransitionConflictError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    assert cp.dispatched == []
    assert uow.commits == 0


async def test_restart_when_stopped_is_conflict() -> None:
    community, server_id, _ = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community_id=community, server_id=server_id))
    use_case = RestartServer(
        uow=uow, control_plane=FakeControlPlane(), clock=FakeClock(_NOW)
    )
    with pytest.raises(InvalidLifecycleTransitionError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )


# --- RCON / server command -------------------------------------------------


async def test_command_forwards_when_running_and_returns_output() -> None:
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.OK, output="players: 3")
    )
    use_case = SendServerCommand(uow=uow, control_plane=cp)

    output = await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        line="list",
    )

    assert output == "players: 3"
    assert cp.dispatched == [("command", WorkerId(worker), ServerId(server_id))]


async def test_command_when_not_running_is_conflict() -> None:
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.STARTING,
            worker_id=worker,
        )
    )
    use_case = SendServerCommand(uow=uow, control_plane=FakeControlPlane())
    with pytest.raises(ServerNotRunningError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            line="list",
        )


# --- reconciler re-dispatch paths (issue #101) -----------------------------


def _start_server(
    uow: FakeUnitOfWork, cp: FakeControlPlane, *, store_generation: int = 0
) -> StartServer:
    return StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(generation=store_generation),
    )


async def test_place_and_start_assigns_an_orphan_and_dispatches() -> None:
    # desired=running with no assigned worker: place + dispatch, keeping desired.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.UNKNOWN,
            worker_id=None,
        )
    )
    cp = FakeControlPlane(place_to=WorkerId(worker))
    result = await _start_server(uow, cp).place_and_start(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    assert result.assigned_worker_id == WorkerId(worker)
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]
    assert cp.incremented == [WorkerId(worker)]
    assert uow.servers.by_id[ServerId(server_id)].desired_state is DesiredState.RUNNING


async def test_place_and_start_releases_reservation_when_commit_raises() -> None:
    # The leak fix applies to place_and_start too (#778): a raising commit between the
    # CAS and the confirm must release the reservation, not leak the slot forever.
    community, server_id, worker = _ids()

    class _FailingCommitUnitOfWork(FakeUnitOfWork):
        async def commit(self) -> None:
            raise RuntimeError("commit failed")

    uow = _FailingCommitUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.UNKNOWN,
            worker_id=None,
        )
    )
    cp = FakeControlPlane(place_to=WorkerId(worker))

    with pytest.raises(RuntimeError, match="commit failed"):
        await _start_server(uow, cp).place_and_start(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    # The reservation was released and never confirmed: no slot leak.
    assert cp.released == [(WorkerId(worker), ServerId(server_id))]
    assert cp.incremented == []


async def test_start_sources_memory_limit_from_config_as_bytes() -> None:
    # The per-server memory limit lives in the config blob as MiB (#705); the start
    # flow must source it via the helper and convert MiB -> bytes for the wire (#706).
    from mc_server_dashboard_api.servers.domain.memory_limit import (
        MEMORY_LIMIT_CONFIG_KEY,
    )

    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.UNKNOWN,
            worker_id=None,
            config={MEMORY_LIMIT_CONFIG_KEY: 2048},
        )
    )
    cp = FakeControlPlane(place_to=WorkerId(worker))
    await _start_server(uow, cp).place_and_start(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    assert cp.start_memory_limit_bytes == 2048 * 1024 * 1024


async def test_start_sends_zero_memory_limit_when_unset() -> None:
    # No limit key -> 0 on the wire, so the Worker driver keeps picking a default
    # heap (pre-#706 behavior unchanged).
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.UNKNOWN,
            worker_id=None,
        )
    )
    cp = FakeControlPlane(place_to=WorkerId(worker))
    await _start_server(uow, cp).place_and_start(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    assert cp.start_memory_limit_bytes == 0


async def test_start_sources_cpu_millis_from_config() -> None:
    # The per-server CPU allocation lives in the config blob as millicores (#722);
    # the start flow sources it via the helper and carries it as-is (#723). No
    # derivation (unlike the memory -> -Xmx path).
    from mc_server_dashboard_api.servers.domain.cpu_allocation import (
        CPU_ALLOCATION_CONFIG_KEY,
    )

    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.UNKNOWN,
            worker_id=None,
            config={CPU_ALLOCATION_CONFIG_KEY: 2000},
        )
    )
    cp = FakeControlPlane(place_to=WorkerId(worker))
    await _start_server(uow, cp).place_and_start(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    assert cp.start_cpu_millis == 2000


async def test_start_sends_zero_cpu_millis_when_unset() -> None:
    # No allocation key -> 0 on the wire, so the Worker driver applies its default
    # weight (existing servers unaffected).
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.UNKNOWN,
            worker_id=None,
        )
    )
    cp = FakeControlPlane(place_to=WorkerId(worker))
    await _start_server(uow, cp).place_and_start(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    assert cp.start_cpu_millis == 0


async def test_place_and_start_always_hydrates_even_if_worker_holds_working_set() -> (
    None
):
    # The skip-hydrate gate is for same-worker restart ONLY (redispatch_start).
    # A fresh placement ALWAYS hydrates, even when the chosen Worker reports holding
    # the working set: a server that moved A->B->A returns via place_and_start, and
    # A's leftover scratch is STALE (B advanced + snapshotted it). Starting on stale
    # leftover scratch is the opposite regression the fix must avoid (issue #696).
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.UNKNOWN,
            worker_id=None,
        )
    )
    cp = FakeControlPlane(
        place_to=WorkerId(worker),
        held={(WorkerId(worker), ServerId(server_id)): 5},
    )
    await _start_server(uow, cp).place_and_start(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    # Hydrate is dispatched despite the held report — place_and_start never skips.
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]


async def test_place_and_start_pre_dispatch_failure_unassigns() -> None:
    # A failure BEFORE the start command is sent (here a failed hydrate) must NOT
    # flip desired to stopped (the running intent is authoritative) but IS safe to
    # unassign so a later tick re-places: no start ever reached a Worker (#101).
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.UNKNOWN,
            worker_id=None,
        )
    )
    cp = FakeControlPlane(
        place_to=WorkerId(worker),
        outcomes={
            "hydrate": CommandOutcome(status=CommandStatus.INTERNAL, message="boom")
        },
    )
    with pytest.raises(CommandDispatchError):
        await _start_server(uow, cp).place_and_start(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.desired_state is DesiredState.RUNNING
    assert stored.assigned_worker_id is None
    assert cp.decremented == [WorkerId(worker)]
    # The start was never sent.
    assert [k for k, _, _ in cp.dispatched] == ["hydrate"]


async def test_place_and_start_lost_response_keeps_assignment() -> None:
    # The lost-response case (#101): hydrate succeeds, then the start command is
    # SENT but its response is lost (WorkerUnavailableError, i.e. CommandTimedOut).
    # The Worker MAY have applied it, so the assignment MUST stick (no unassign,
    # no decrement) — the next tick redispatches to the SAME Worker, never re-places
    # on a different one. Re-placing here would spawn a second live instance.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.UNKNOWN,
            worker_id=None,
        )
    )
    cp = FakeControlPlane(place_to=WorkerId(worker), unavailable_kinds={"start"})
    with pytest.raises(WorkerUnavailableError):
        await _start_server(uow, cp).place_and_start(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.desired_state is DesiredState.RUNNING
    # Assignment retained: the started server stays on the SAME Worker.
    assert stored.assigned_worker_id == WorkerId(worker)
    assert cp.decremented == []
    # The start command was attempted (sent) before the response was lost.
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]


async def test_place_and_start_failed_start_outcome_keeps_assignment() -> None:
    # A failed START outcome (not an exception) may also reflect a partially-applied
    # command, so the assignment sticks for a same-Worker redispatch (#101).
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.UNKNOWN,
            worker_id=None,
        )
    )
    cp = FakeControlPlane(
        place_to=WorkerId(worker),
        outcomes={
            "start": CommandOutcome(status=CommandStatus.INTERNAL, message="boom")
        },
    )
    with pytest.raises(CommandDispatchError):
        await _start_server(uow, cp).place_and_start(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.desired_state is DesiredState.RUNNING
    assert stored.assigned_worker_id == WorkerId(worker)
    assert cp.decremented == []


async def test_redispatch_start_replays_launch_without_increment() -> None:
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.CRASHED,
            worker_id=worker,
        )
    )
    cp = FakeControlPlane()
    result = await _start_server(uow, cp).redispatch_start(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]
    assert cp.incremented == []
    # Genuine success writes NO observed state: the Worker's StatusChange(running)
    # converges the cache (issue #213). The seeded observed/observed_at stand.
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.observed_state is ObservedState.CRASHED
    assert stored.observed_at is None
    assert result.observed_state is ObservedState.CRASHED


async def test_redispatch_start_skips_hydrate_when_held_generation_is_fresh() -> None:
    # Generation-gated skip-hydrate (issue #763): a same-worker restart whose
    # assigned Worker reports holding a generation AT LEAST the store generation must
    # NOT hydrate — the hydrate would clobber the newer scratch with the last
    # snapshot and roll the world back. Start is still dispatched.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.STOPPED,
            worker_id=worker,
        )
    )
    # Worker holds generation 5, equal to the store generation -> skip hydrate.
    cp = FakeControlPlane(
        held={(WorkerId(worker), ServerId(server_id)): 5},
    )
    await _start_server(uow, cp, store_generation=5).redispatch_start(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    # Hydrate is SKIPPED; only start is dispatched.
    assert [k for k, _, _ in cp.dispatched] == ["start"]


async def test_redispatch_start_hydrates_when_held_generation_is_stale() -> None:
    # Presence at a STALE generation must hydrate (issue #763): an A->B->A leftover
    # scratch is present but older than the store generation B advanced past, so the
    # reconciler hydrates rather than starting on the stale working set.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.STOPPED,
            worker_id=worker,
        )
    )
    # Worker holds generation 3, older than the store generation 8 -> hydrate.
    cp = FakeControlPlane(
        held={(WorkerId(worker), ServerId(server_id)): 3},
    )
    await _start_server(uow, cp, store_generation=8).redispatch_start(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]


async def test_redispatch_start_hydrates_when_worker_does_not_hold_working_set() -> (
    None
):
    # The assigned Worker does NOT report holding the working set (a fresh/wiped/GC'd
    # scratch, or a Worker too old to report): hydrate then start, so a same-worker
    # restart never silently boots an empty/absent working set (issue #763).
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.STOPPED,
            worker_id=worker,
        )
    )
    # No `held` entry -> held_generation is None -> hydrate.
    cp = FakeControlPlane()
    await _start_server(uow, cp, store_generation=2).redispatch_start(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]


async def test_redispatch_start_invalid_state_is_treated_as_running() -> None:
    # The worker rejects a launch on an already-running instance with INVALID_STATE
    # (hydrate/start guards). That means the server is in fact running -> the
    # divergence is resolved; do NOT flip the running intent to stopped.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.UNKNOWN,
            worker_id=worker,
        )
    )
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.INVALID_STATE, message="running")
    )
    result = await _start_server(uow, cp).redispatch_start(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    assert result.desired_state is DesiredState.RUNNING
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.desired_state is DesiredState.RUNNING
    # INVALID_STATE is positive evidence the instance is live, and the worker does
    # no transition so no StatusChange will ever arrive to repair observed: record
    # observed=running here, on the row and the returned entity, or the reconciler
    # re-selects this divergence and redispatches forever (issue #213). No unassign:
    # the live instance keeps its Worker.
    assert stored.observed_state is ObservedState.RUNNING
    assert stored.observed_at == _NOW
    assert stored.assigned_worker_id == WorkerId(worker)
    assert result.observed_state is ObservedState.RUNNING
    assert result.observed_at == _NOW


async def test_redispatch_start_invalid_state_returned_entity_honest_when_dropped() -> (
    None
):
    # Honesty fix (issue #292): under a same-instant clock the #216 guard drops the
    # observed=running convergence write. The returned entity must reflect the
    # DROPPED write, not optimistically claim observed=running that did not land.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    seeded = _server(
        community_id=community,
        server_id=server_id,
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker_id=worker,
    )
    # A fresher write already stamped the row at the clock's instant; the guard drops
    # the equal-stamped convergence write.
    seeded.observed_at = _NOW
    uow.servers.seed(seeded)
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.INVALID_STATE, message="running")
    )

    result = await _start_server(uow, cp).redispatch_start(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    stored = uow.servers.by_id[ServerId(server_id)]
    # The guard dropped the write, so the row keeps its observed cache.
    assert stored.observed_state is ObservedState.UNKNOWN
    # The returned entity must agree with the row, not the optimistic mutation.
    assert result.observed_state is ObservedState.UNKNOWN


async def test_redispatch_start_busy_does_not_record_observed_running() -> None:
    # BUSY (issue #824): the Worker refused the start because another lifecycle
    # command is already in flight for this id and its outcome is UNKNOWN -- unlike
    # INVALID_STATE (already-running), this is NOT positive evidence the instance is
    # live. So redispatch_start must NOT record observed=running (the bug: a raced
    # original that later FAILS would leave a speculative observed=running stuck on a
    # down server). It raises instead, writing no row, so the assignment + running
    # intent stand and a later tick retries once the in-flight command settles.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.UNKNOWN,
            worker_id=worker,
        )
    )
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.BUSY, message="in flight")
    )
    with pytest.raises(CommandDispatchError):
        await _start_server(uow, cp).redispatch_start(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )
    stored = uow.servers.by_id[ServerId(server_id)]
    # No convergence write: observed stays UNKNOWN, not RUNNING.
    assert stored.observed_state is ObservedState.UNKNOWN
    # Intent and assignment are untouched for the next reconcile tick's retry.
    assert stored.desired_state is DesiredState.RUNNING
    assert stored.assigned_worker_id == WorkerId(worker)


async def test_redispatch_start_failure_keeps_running_intent() -> None:
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.CRASHED,
            worker_id=worker,
        )
    )
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.INTERNAL, message="boom")
    )
    with pytest.raises(CommandDispatchError):
        await _start_server(uow, cp).redispatch_start(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )
    stored = uow.servers.by_id[ServerId(server_id)]
    # No DB write happened; desired/assignment are untouched for the next tick.
    assert stored.desired_state is DesiredState.RUNNING
    assert stored.assigned_worker_id == WorkerId(worker)


async def test_redispatch_stop_replays_stop_without_decrement() -> None:
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.STOPPED,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )
    cp = FakeControlPlane()
    await StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW)).redispatch_stop(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    # A confirmed stop takes the same post-stop final snapshot as StopServer.__call__
    # (issue #846); the decrement is not repeated here.
    assert [k for k, _, _ in cp.dispatched] == ["stop", "snapshot"]
    assert cp.decremented == []
    # A confirmed stop clears the assignment so a later start can re-place it
    # (issue #206); the decrement is not repeated here.
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.assigned_worker_id is None


async def test_redispatch_stop_server_not_found_unassigns() -> None:
    # SERVER_NOT_FOUND on redispatch means no live instance remains: converge by
    # clearing the assignment, same as the success path (issue #206).
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.STOPPED,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )
    cp = FakeControlPlane(
        outcomes={"stop": CommandOutcome(status=CommandStatus.SERVER_NOT_FOUND)}
    )
    await StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW)).redispatch_stop(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.assigned_worker_id is None
    # No live instance remained, so there is no working set to capture: no snapshot
    # is dispatched on the SERVER_NOT_FOUND path (issue #846).
    assert [k for k, _, _ in cp.dispatched] == ["stop"]


async def test_redispatch_stop_takes_final_snapshot_on_success() -> None:
    # A confirmed reconciler-driven stop must take the same post-stop final snapshot
    # StopServer.__call__ takes, targeting the assigned Worker and the (community,
    # server) scope (issue #846, FR-DATA-7) — otherwise the world progression since
    # the last periodic snapshot is lost and the Worker strands the stop scratch.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.STOPPED,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )

    class _RecordsScope(FakeControlPlane):
        async def snapshot(
            self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
        ) -> CommandOutcome:
            self.snapshot_scope = (worker_id, community_id, server_id)
            return await super().snapshot(
                worker_id=worker_id, community_id=community_id, server_id=server_id
            )

    cp = _RecordsScope()
    await StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW)).redispatch_stop(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    assert ("snapshot", WorkerId(worker), ServerId(server_id)) in cp.dispatched
    assert cp.snapshot_scope == (
        WorkerId(worker),
        CommunityId(community),
        ServerId(server_id),
    )


async def test_redispatch_stop_holds_assignment_until_final_snapshot_settles() -> None:
    # Issue #847: the SECOND _final_snapshot call site (redispatch_stop) must hold
    # the assignment until the snapshot settles too, exactly like StopServer.__call__
    # -- otherwise a reconciler-driven final stop has the same cross-worker re-place
    # race.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.STOPPED,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )

    class _AssertsHeldDuringSnapshot(FakeControlPlane):
        async def snapshot(
            self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
        ) -> CommandOutcome:
            row = uow.servers.by_id[server_id]
            self.assignment_at_snapshot = row.assigned_worker_id
            return await super().snapshot(
                worker_id=worker_id, community_id=community_id, server_id=server_id
            )

    cp = _AssertsHeldDuringSnapshot()
    result = await StopServer(
        uow=uow, control_plane=cp, clock=FakeClock(_NOW)
    ).redispatch_stop(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    assert cp.assignment_at_snapshot == WorkerId(worker)
    assert result.assigned_worker_id is None
    assert uow.servers.by_id[ServerId(server_id)].assigned_worker_id is None
    assert [kind for kind, _, _ in cp.dispatched] == ["stop", "snapshot"]


async def test_redispatch_stop_final_snapshot_failure_logs_error_and_converges(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A failing final snapshot must not fail the reconciler tick: the stop already
    # succeeded and the server is down. The failure is logged LOUD (error level,
    # issue #841) but convergence (observed=stopped, unassigned) still lands (#846).
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.STOPPED,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )

    class _SnapshotFails(FakeControlPlane):
        async def snapshot(
            self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
        ) -> CommandOutcome:
            self.dispatched.append(("snapshot", worker_id, server_id))
            return CommandOutcome(
                status=CommandStatus.TRANSFER_FAILED, message="empty_snapshot"
            )

    cp = _SnapshotFails()
    with caplog.at_level(logging.ERROR):
        result = await StopServer(
            uow=uow, control_plane=cp, clock=FakeClock(_NOW)
        ).redispatch_stop(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    # The tick did not raise and convergence is unaffected by the snapshot failure.
    assert result.observed_state is ObservedState.STOPPED
    assert result.assigned_worker_id is None
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.assigned_worker_id is None
    record = next(
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "final snapshot" in r.getMessage()
    )
    assert str(server_id) in record.getMessage()


async def test_redispatch_stop_returned_entity_honest_when_observed_write_dropped() -> (
    None
):
    # Honesty fix (issue #292) + improvement (issue #847 round-3): under a same-instant
    # clock the #216 guard drops the observed=stopped convergence write, so the
    # observed cache must NOT optimistically claim stopped. BUT the deferred assignment
    # clear is independently CAS-guarded (``clear_assignment_after_final_snapshot``
    # matches only a still desired=stopped row still assigned to this worker) and runs
    # regardless of the dropped observed write: post-#847 no other path unassigns, so
    # running it cannot clobber a fresher write and removes the wedge the old
    # applied-gate left for the full grace window.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    seeded = _server(
        community_id=community,
        server_id=server_id,
        desired=DesiredState.STOPPED,
        observed=ObservedState.RUNNING,
        worker_id=worker,
    )
    # A fresher write already stamped the row at the clock's instant; the guard drops
    # the equal-stamped observed-convergence write.
    seeded.observed_at = _NOW
    uow.servers.seed(seeded)
    cp = FakeControlPlane()

    result = await StopServer(
        uow=uow, control_plane=cp, clock=FakeClock(_NOW)
    ).redispatch_stop(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    stored = uow.servers.by_id[ServerId(server_id)]
    # The guard dropped the observed write, so the row keeps its observed cache, and
    # the returned entity must agree (not the optimistic mutation).
    assert stored.observed_state is ObservedState.RUNNING
    assert result.observed_state is ObservedState.RUNNING
    # The CAS-guarded clear still ran (desired=stopped + same worker), so the
    # assignment is released — no grace-bounded wedge.
    assert stored.assigned_worker_id is None
    assert result.assigned_worker_id is None


async def test_redispatch_stop_failure_keeps_assignment() -> None:
    # A failed redispatch leaves the process possibly alive: keep the assignment
    # so a later tick retries the SAME Worker (issue #206 stickiness).
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.STOPPED,
            observed=ObservedState.RUNNING,
            worker_id=worker,
        )
    )
    cp = FakeControlPlane(
        outcomes={"stop": CommandOutcome(status=CommandStatus.INTERNAL, message="boom")}
    )
    with pytest.raises(CommandDispatchError):
        await StopServer(
            uow=uow, control_plane=cp, clock=FakeClock(_NOW)
        ).redispatch_stop(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )
    stored = uow.servers.by_id[ServerId(server_id)]
    assert stored.assigned_worker_id == WorkerId(worker)
