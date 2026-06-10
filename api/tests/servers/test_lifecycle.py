"""Use-case tests for the server lifecycle (start/stop/restart/RCON, Section 6.5).

Drives the use cases against in-memory fakes (no DB, no gRPC): the transition
matrix (start-when-running, stop-when-stopped, restart-when-stopped all conflict),
placement failure (no eligible worker), dispatch-failure compensation (the
committed start intent is honestly reverted), RCON forwarding (only when observed
running), and the placement-load increment/decrement bookkeeping.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

import pytest

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
    store_generation: int = 0,
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
        store_generation=store_generation,
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
        store_generation=FakeStoreGenerationReader(uow.servers),
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
        store_generation=FakeStoreGenerationReader(uow.servers),
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
        store_generation=FakeStoreGenerationReader(uow.servers),
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
        store_generation=FakeStoreGenerationReader(uow.servers),
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
        store_generation=FakeStoreGenerationReader(uow.servers),
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
        store_generation=FakeStoreGenerationReader(uow.servers),
    )

    with pytest.raises(NoEligibleWorkerError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )
    # Nothing was committed or dispatched.
    assert uow.servers.by_id[ServerId(server_id)].desired_state is DesiredState.STOPPED
    assert cp.dispatched == []


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
        store_generation=FakeStoreGenerationReader(uow.servers),
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
    # Hydrate succeeds, then the launch fails: both dispatches ran, and the
    # committed intent must still be compensated.
    busy = CommandOutcome(status=CommandStatus.INVALID_STATE, message="busy")
    cp = FakeControlPlane(place_to=WorkerId(worker), outcomes={"start": busy})
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(uow.servers),
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
    busy = CommandOutcome(status=CommandStatus.INVALID_STATE, message="instance busy")
    cp = FakeControlPlane(place_to=WorkerId(worker), outcomes={"start": busy})
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(uow.servers),
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
    busy = CommandOutcome(status=CommandStatus.INVALID_STATE, message="busy")
    cp = FakeControlPlane(place_to=WorkerId(worker), outcomes={"start": busy})
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(uow.servers),
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
    busy = CommandOutcome(status=CommandStatus.INVALID_STATE, message="busy")
    cp = FakeControlPlane(place_to=WorkerId(worker), outcomes={"start": busy})
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(uow.servers),
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
        store_generation=FakeStoreGenerationReader(uow.servers),
    )

    with pytest.raises(LifecycleTransitionConflictError):
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    # No dispatch, no commit, no count change: the winner's row is untouched.
    assert cp.dispatched == []
    assert cp.incremented == []
    assert cp.decremented == []
    assert uow.commits == 0
    survivor = uow.servers.by_id[ServerId(server_id)]
    assert survivor.assigned_worker_id == WorkerId(winner_worker)


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
        store_generation=FakeStoreGenerationReader(uow.servers),
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
        store_generation=FakeStoreGenerationReader(uow.servers),
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
        store_generation=FakeStoreGenerationReader(uow.servers),
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
        store_generation=FakeStoreGenerationReader(uow.servers),
    )(community_id=CommunityId(community), server_id=ServerId(server_id))

    assert started.desired_state is DesiredState.RUNNING
    assert started.assigned_worker_id == WorkerId(next_worker)


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
        store_generation=FakeStoreGenerationReader(uow.servers),
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


def _start_server(uow: FakeUnitOfWork, cp: FakeControlPlane) -> StartServer:
    return StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(uow.servers),
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
            store_generation=5,
        )
    )
    # Worker holds generation 5, equal to the store generation -> skip hydrate.
    cp = FakeControlPlane(
        held={(WorkerId(worker), ServerId(server_id)): 5},
    )
    await _start_server(uow, cp).redispatch_start(
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
            store_generation=8,
        )
    )
    # Worker holds generation 3, older than the store generation 8 -> hydrate.
    cp = FakeControlPlane(
        held={(WorkerId(worker), ServerId(server_id)): 3},
    )
    await _start_server(uow, cp).redispatch_start(
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
            store_generation=2,
        )
    )
    # No `held` entry -> held_generation is None -> hydrate.
    cp = FakeControlPlane()
    await _start_server(uow, cp).redispatch_start(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]


async def test_redispatch_start_hydrates_when_db_mirror_lags_storage_generation() -> (
    None
):
    # The threshold is Storage's authoritative generation, NOT the lag-prone
    # server.store_generation DB mirror (issue #763). Model the dangerous window: a
    # snapshot durably advanced Storage to generation 8, but the SEPARATE DB-mirror
    # write that records it onto the row failed, so the row still reads 5. A Worker
    # holding generation 5 would pass `held >= db_mirror` (5 >= 5) and WRONGLY skip a
    # hydrate it needs, rolling the world back to the generation-5 snapshot. Comparing
    # against Storage (8) makes 5 < 8 -> hydrate, closing the window.
    community, server_id, worker = _ids()
    uow = FakeUnitOfWork()
    uow.servers.seed(
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.STOPPED,
            worker_id=worker,
            store_generation=5,  # the lagging DB mirror
        )
    )
    cp = FakeControlPlane(
        held={(WorkerId(worker), ServerId(server_id)): 5},
    )
    use_case = StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        # Storage's authoritative generation is ahead of the DB mirror.
        store_generation=FakeStoreGenerationReader(generation=8),
    )
    await use_case.redispatch_start(
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
    assert [k for k, _, _ in cp.dispatched] == ["stop"]
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


async def test_redispatch_stop_returned_entity_honest_when_write_dropped() -> None:
    # Honesty fix (issue #292): under a same-instant clock the #216 guard drops the
    # observed=stopped/unassign convergence write. The returned entity must reflect
    # the DROPPED write, not optimistically claim observed=stopped / unassigned.
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
    # the equal-stamped convergence write (and its unassign, atomically).
    seeded.observed_at = _NOW
    uow.servers.seed(seeded)
    cp = FakeControlPlane()

    result = await StopServer(
        uow=uow, control_plane=cp, clock=FakeClock(_NOW)
    ).redispatch_stop(
        community_id=CommunityId(community), server_id=ServerId(server_id)
    )

    stored = uow.servers.by_id[ServerId(server_id)]
    # The guard dropped the write, so the row keeps its observed cache and assignment.
    assert stored.observed_state is ObservedState.RUNNING
    assert stored.assigned_worker_id == WorkerId(worker)
    # The returned entity must agree with the row, not the optimistic mutation.
    assert result.observed_state is ObservedState.RUNNING
    assert result.assigned_worker_id == WorkerId(worker)


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
