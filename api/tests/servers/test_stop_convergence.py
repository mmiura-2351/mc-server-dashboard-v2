"""Overlapping stop convergence for one server (issue #3491).

An operator stop of a still-assigned desired-stopped server skips the reconciler's
grace, so two snapshot-then-release flows for the same server can now meet: the
original stop's, another operator stop's, the reconciler's, and the late result of
an abandoned snapshot. The release's compare-and-set cannot tell whose snapshot
the hold is waiting on, so one flow's outcome must never release while another
flow's upload is still in flight. Each test holds one flow's final snapshot open,
drives a second flow into it, and pins that the second neither dispatches nor
releases. The flows are separate ``StopServer`` instances, each with its own unit
of work over one shared repository, as in production.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.servers.application.lifecycle import StopServer
from mc_server_dashboard_api.servers.application.reconciler import RunReconcilerTick
from mc_server_dashboard_api.servers.application.stop_convergence import (
    StopConvergenceGuard,
)
from mc_server_dashboard_api.servers.application.stop_dispatch_refusals import (
    StopDispatchRefusals,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    LifecycleTransitionConflictError,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
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
    FakeStoreGenerationReader,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_LONG_AGO = _NOW - dt.timedelta(hours=1)
_FAILED = CommandOutcome(
    status=CommandStatus.TRANSFER_FAILED, message="upload aborted by transfer bound"
)


class _HeldFirstSnapshot(FakeControlPlane):
    """The first snapshot stays in flight until ``finish``; later ones return OK."""

    def __init__(self, *, first: CommandOutcome) -> None:
        super().__init__()
        self._first = first
        self._gate = asyncio.Event()
        self.first_started = asyncio.Event()
        self._snapshots = 0

    def finish(self) -> None:
        self._gate.set()

    def snapshot_count(self) -> int:
        return [kind for kind, _, _ in self.dispatched].count("snapshot")

    async def snapshot(
        self,
        *,
        worker_id: WorkerId,
        community_id: CommunityId,
        server_id: ServerId,
        final: bool = False,
    ) -> CommandOutcome:
        self._snapshots += 1
        outcome = await super().snapshot(
            worker_id=worker_id,
            community_id=community_id,
            server_id=server_id,
            final=final,
        )
        if self._snapshots > 1:
            return outcome
        self.first_started.set()
        await self._gate.wait()
        return self._first


def _server(*, desired: DesiredState, observed: ObservedState) -> Server:
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=desired,
        observed_state=observed,
        observed_at=_LONG_AGO,
        assigned_worker_id=WorkerId(uuid.uuid4()),
        created_at=_LONG_AGO,
        updated_at=_LONG_AGO,
    )


def _stop_server(
    repo: FakeServerRepository, cp: FakeControlPlane, guard: StopConvergenceGuard
) -> StopServer:
    return StopServer(
        uow=FakeUnitOfWork(servers=repo),
        control_plane=cp,
        clock=FakeClock(_NOW),
        stop_convergence=guard,
    )


def _assigned(repo: FakeServerRepository, server: Server) -> WorkerId | None:
    return repo.by_id[server.id].assigned_worker_id


async def test_second_operator_stop_refuses_while_the_first_snapshot_is_in_flight() -> (
    None
):
    # The first stop's snapshot fails (TRANSFER_FAILED, so the scratch stays). A
    # second stop would queue a live retry upload over that scratch behind it,
    # and the first stop's release would then land mid-upload. Refuse the second.
    server = _server(desired=DesiredState.STOPPED, observed=ObservedState.STOPPED)
    repo = FakeServerRepository()
    repo.seed(server)
    guard = StopConvergenceGuard()
    cp = _HeldFirstSnapshot(first=_FAILED)
    first = asyncio.create_task(
        _stop_server(repo, cp, guard)(
            community_id=server.community_id, server_id=server.id
        )
    )
    await cp.first_started.wait()

    with pytest.raises(LifecycleTransitionConflictError):
        await _stop_server(repo, cp, guard)(
            community_id=server.community_id, server_id=server.id
        )

    assert cp.snapshot_count() == 1
    assert _assigned(repo, server) == server.assigned_worker_id
    cp.finish()
    await first
    # The release is the first flow's, on its own (failed) snapshot outcome.
    assert _assigned(repo, server) is None


async def test_operator_stop_refuses_while_the_original_stop_snapshots() -> None:
    # The original stop confirmed and is still uploading its final snapshot; the
    # row already reads (stopped, stopped, assigned).
    server = _server(desired=DesiredState.RUNNING, observed=ObservedState.RUNNING)
    repo = FakeServerRepository()
    repo.seed(server)
    guard = StopConvergenceGuard()
    cp = _HeldFirstSnapshot(first=CommandOutcome(status=CommandStatus.OK))
    original = asyncio.create_task(
        _stop_server(repo, cp, guard)(
            community_id=server.community_id, server_id=server.id
        )
    )
    await cp.first_started.wait()

    with pytest.raises(LifecycleTransitionConflictError):
        await _stop_server(repo, cp, guard)(
            community_id=server.community_id, server_id=server.id
        )

    assert cp.snapshot_count() == 1
    assert _assigned(repo, server) == server.assigned_worker_id
    cp.finish()
    await original
    assert _assigned(repo, server) is None


async def test_reconciler_skips_a_row_an_operator_stop_is_converging() -> None:
    # The row is past the reconciler's grace, so without the exclusion its tick
    # would run clear_stale_assignment and release over the operator's upload.
    server = _server(desired=DesiredState.STOPPED, observed=ObservedState.STOPPED)
    repo = FakeServerRepository()
    repo.seed(server)
    guard = StopConvergenceGuard()
    cp = _HeldFirstSnapshot(first=_FAILED)
    clock = FakeClock(_NOW)
    refusals = StopDispatchRefusals()
    reconciler = RunReconcilerTick(
        uow=FakeUnitOfWork(servers=repo),
        make_start_server=lambda: pytest.fail("no start is owed"),
        make_stop_server=lambda: _stop_server(repo, cp, guard),
        control_plane=cp,
        store_generation=FakeStoreGenerationReader(),
        clock=clock,
        stop_refusals=refusals,
        grace_seconds=60,
        held_start_grace_seconds=10,
        refused_stop_grace_seconds=5,
        restart_timeout_seconds=120,
        backoff_base_seconds=30,
        backoff_max_seconds=3600,
    )
    operator = asyncio.create_task(
        _stop_server(repo, cp, guard)(
            community_id=server.community_id, server_id=server.id
        )
    )
    await cp.first_started.wait()

    await reconciler.tick()

    assert cp.snapshot_count() == 1
    assert _assigned(repo, server) == server.assigned_worker_id
    # Skipped, not failed: no backoff is armed for the row.
    assert server.id not in reconciler._attempts
    cp.finish()
    await operator
    assert _assigned(repo, server) is None


async def test_late_result_does_not_release_while_another_flow_snapshots() -> None:
    # An earlier snapshot was abandoned (timed out), leaving the row held. An
    # operator stop is now uploading its own retry when the abandoned snapshot's
    # late result arrives: only the operator flow's outcome may release.
    server = _server(desired=DesiredState.STOPPED, observed=ObservedState.STOPPED)
    repo = FakeServerRepository()
    repo.seed(server)
    guard = StopConvergenceGuard()
    cp = _HeldFirstSnapshot(first=CommandOutcome(status=CommandStatus.OK))
    operator = asyncio.create_task(
        _stop_server(repo, cp, guard)(
            community_id=server.community_id, server_id=server.id
        )
    )
    await cp.first_started.wait()
    assert server.assigned_worker_id is not None

    await _stop_server(repo, cp, guard).clear_assignment_after_late_snapshot(
        server_id=server.id,
        worker_id=server.assigned_worker_id,
        succeeded=False,
        message="upload aborted by transfer bound",
    )

    assert _assigned(repo, server) == server.assigned_worker_id
    cp.finish()
    await operator
    assert _assigned(repo, server) is None


async def test_late_result_releases_when_no_flow_is_converging() -> None:
    # The exclusion only defers to a flow in flight; with none, the late result
    # still releases the held row at once (issue #891).
    server = _server(desired=DesiredState.STOPPED, observed=ObservedState.STOPPED)
    repo = FakeServerRepository()
    repo.seed(server)
    guard = StopConvergenceGuard()
    assert server.assigned_worker_id is not None

    await _stop_server(
        repo, FakeControlPlane(), guard
    ).clear_assignment_after_late_snapshot(
        server_id=server.id,
        worker_id=server.assigned_worker_id,
        succeeded=True,
        message=None,
    )

    assert _assigned(repo, server) is None
    assert not guard.in_flight(server.id)
