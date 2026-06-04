"""Use-case tests for the server lifecycle (start/stop/restart/RCON, Section 6.5).

Drives the use cases against in-memory fakes (no DB, no gRPC): the transition
matrix (start-when-running, stop-when-stopped, restart-when-stopped all conflict),
placement failure (no eligible worker), dispatch-failure compensation (the
committed start intent is honestly reverted), RCON forwarding (only when observed
running), and the placement-load increment/decrement bookkeeping.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.servers.application.lifecycle import (
    RestartServer,
    SendServerCommand,
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
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
from mc_server_dashboard_api.servers.domain.value_objects import (
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
    FakeServerRepository,
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
) -> Server:
    return Server(
        id=ServerId(server_id),
        community_id=CommunityId(community_id),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.HOST_PROCESS,
        config={},
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
    use_case = StartServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

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
        uow=uow, control_plane=FakeControlPlane(), clock=FakeClock(_NOW)
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
    use_case = StartServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

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
    use_case = StartServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

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
    use_case = StartServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

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
    use_case = StartServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

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
    use_case = StartServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

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


async def test_start_compensation_failure_preserves_both_errors() -> None:
    # Dispatch fails, then the compensation commit also fails. The compensation
    # error must propagate chained from the original dispatch failure so neither
    # is masked.
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
    use_case = StartServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))

    with pytest.raises(RuntimeError, match="compensation commit failed") as excinfo:
        await use_case(
            community_id=CommunityId(community), server_id=ServerId(server_id)
        )

    # The original dispatch failure is preserved as the chained cause.
    assert isinstance(excinfo.value.__cause__, CommandDispatchError)


async def test_start_missing_server_is_not_found() -> None:
    community, server_id, _ = _ids()
    use_case = StartServer(
        uow=FakeUnitOfWork(), control_plane=FakeControlPlane(), clock=FakeClock(_NOW)
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
        uow=uow, control_plane=FakeControlPlane(), clock=FakeClock(_NOW)
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
