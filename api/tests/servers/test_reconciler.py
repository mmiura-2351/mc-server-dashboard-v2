"""Use-case tests for the desired/observed divergence reconciler (issue #101).

Drives :class:`RunReconcilerTick` against in-memory fakes with a faked clock over
the divergence matrix: each actionable case re-dispatches the right command, the
non-actionable cases are skipped, the grace window suppresses a fresh divergence,
per-server exponential backoff prevents thrash within its window, and a divergence
on a disconnected Worker is skipped (its observed=unknown is expected; the
reconnect rebuild owns it).
"""

from __future__ import annotations

import datetime as dt
import uuid

from mc_server_dashboard_api.servers.application.lifecycle import (
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.application.reconciler import RunReconcilerTick
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
)
from mc_server_dashboard_api.servers.domain.entities import Server
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
    FakeJarProvisioner,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_WORKER = WorkerId(uuid.uuid4())
_GRACE = 60
_PAST_GRACE = _NOW - dt.timedelta(seconds=_GRACE + 1)


def _server(
    *,
    desired: DesiredState,
    observed: ObservedState,
    worker: WorkerId | None,
    observed_at: dt.datetime | None = _PAST_GRACE,
) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.HOST_PROCESS,
        config={},
        desired_state=desired,
        observed_state=observed,
        observed_at=observed_at,
        assigned_worker_id=worker,
        created_at=_PAST_GRACE,
        updated_at=_PAST_GRACE,
    )


def _reconciler(
    uow: FakeUnitOfWork,
    cp: FakeControlPlane,
    clock: FakeClock,
) -> RunReconcilerTick:
    return RunReconcilerTick(
        uow=uow,
        start_server=StartServer(
            uow=uow,
            control_plane=cp,
            clock=clock,
            jar_provisioner=FakeJarProvisioner(),
        ),
        stop_server=StopServer(uow=uow, control_plane=cp, clock=clock),
        control_plane=cp,
        clock=clock,
        grace_seconds=_GRACE,
        backoff_base_seconds=30,
        backoff_max_seconds=3600,
    )


# --- actionable divergences ------------------------------------------------


async def test_running_intent_stale_observed_redispatches_start() -> None:
    # desired=running, assigned, observed crashed past grace, worker connected:
    # re-send hydrate-then-start (no new placement / increment).
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]
    assert cp.incremented == []  # assignment already counted on the original start


async def test_running_intent_orphan_places_and_starts() -> None:
    # desired=running with no assigned worker (compensation-failure orphan):
    # run the full placement + dispatch path.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker=None,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(place_to=_WORKER)
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]
    assert cp.incremented == [_WORKER]
    # The orphan is now assigned.
    assert uow.servers.by_id[server.id].assigned_worker_id == _WORKER


async def test_stopped_intent_observed_running_redispatches_stop() -> None:
    # desired=stopped but the worker still reports running: re-send stop.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert [k for k, _, _ in cp.dispatched] == ["stop"]
    assert cp.decremented == []  # the original stop owns the decrement


# --- non-actionable / skipped ---------------------------------------------


async def test_aligned_running_server_is_not_touched() -> None:
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert cp.dispatched == []


async def test_starting_server_is_not_touched() -> None:
    # observed=starting is a normal in-flight start, not a divergence.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.STARTING,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert cp.dispatched == []


async def test_divergence_within_grace_is_skipped() -> None:
    # A divergence reported only moments ago is inside the grace window: give the
    # normal path time to converge before acting.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
        observed_at=_NOW - dt.timedelta(seconds=_GRACE - 1),
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert cp.dispatched == []


async def test_disconnected_worker_is_skipped() -> None:
    # The assigned worker is gone; observed=unknown is expected and the reconnect
    # rebuild owns it. Skip rather than dispatch a doomed command.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(connected={_WORKER: False})
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert cp.dispatched == []


# --- backoff ---------------------------------------------------------------


async def test_failed_action_backs_off_then_retries() -> None:
    # A failed re-dispatch is not retried on the very next tick (backoff window);
    # once the window lapses it is retried.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.INTERNAL, message="boom")
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)
    await reconciler.tick()  # attempt 1 -> fails, backoff 30s
    assert len(cp.dispatched) == 1
    clock.set(_NOW + dt.timedelta(seconds=10))
    await reconciler.tick()  # within backoff -> skipped
    assert len(cp.dispatched) == 1
    clock.set(_NOW + dt.timedelta(seconds=40))
    await reconciler.tick()  # past backoff -> retried
    assert len(cp.dispatched) == 2


async def test_backoff_grows_exponentially() -> None:
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.INTERNAL, message="boom")
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)
    await reconciler.tick()  # fail 1 -> backoff 30s
    clock.set(_NOW + dt.timedelta(seconds=31))
    await reconciler.tick()  # fail 2 -> backoff 60s
    assert len(cp.dispatched) == 2
    clock.set(_NOW + dt.timedelta(seconds=31 + 31))  # only 31s after fail 2 (< 60)
    await reconciler.tick()  # still within the grown window -> skipped
    assert len(cp.dispatched) == 2
    clock.set(_NOW + dt.timedelta(seconds=31 + 61))  # > 60s after fail 2
    await reconciler.tick()  # retried
    assert len(cp.dispatched) == 3


async def test_success_clears_backoff() -> None:
    # After a successful action the per-server backoff entry is cleared, so a
    # later, fresh divergence is acted on immediately (no stale backoff).
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)
    await reconciler.tick()
    assert len(cp.dispatched) == 1
    assert server.id not in reconciler._attempts


async def test_unknown_observed_with_connected_worker_redispatches_start() -> None:
    # desired=running, assigned, observed=unknown but the worker is in fact
    # connected (a stale unknown): treat as a stale start and re-dispatch.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]
