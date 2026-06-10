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
import logging
import uuid

import pytest

from mc_server_dashboard_api.servers.application.lifecycle import (
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.application.reconciler import RunReconcilerTick
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
    WorkerUnavailableError,
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
    FakeStoreGenerationReader,
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
            store_generation=FakeStoreGenerationReader(),
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


async def test_orphan_start_lost_response_never_replaces_on_another_worker() -> None:
    # The double-placement bug (#101): an orphan's start is SENT but its response is
    # lost (WorkerUnavailableError, i.e. CommandTimedOut). The Worker MAY have
    # applied it. The reconciler MUST keep the assignment and, on the next tick,
    # redispatch to the SAME Worker — it must NEVER place the server on a different
    # Worker (which the per-process double-start guard would not catch -> two live
    # instances of one server).
    class _StartLostThenOk(FakeControlPlane):
        """Place once; the first start raises (lost response), later starts succeed."""

        def __init__(self) -> None:
            super().__init__(place_to=_WORKER)
            self.places = 0
            self._start_calls = 0

        async def place(self, **kwargs: object) -> WorkerId:
            self.places += 1
            return _WORKER

        async def start(self, **kwargs: object) -> CommandOutcome:
            self._start_calls += 1
            if self._start_calls == 1:
                self.dispatched.append(("start", _WORKER, kwargs["server_id"]))  # type: ignore[arg-type]
                raise WorkerUnavailableError("lost response")
            return await super().start(**kwargs)  # type: ignore[arg-type]

    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker=None,
    )
    uow.servers.seed(server)
    cp = _StartLostThenOk()
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)

    # Tick 1: orphan placed, start sent, response lost -> assignment RETAINED.
    await reconciler.tick()
    stored = uow.servers.by_id[server.id]
    assert stored.assigned_worker_id == _WORKER
    assert cp.places == 1

    # Tick 2 (past the backoff window): the server is now an ASSIGNED stale-running
    # candidate, so the reconciler takes redispatch_start to the SAME Worker. No
    # second placement happens.
    clock.set(_NOW + dt.timedelta(seconds=3601))
    await reconciler.tick()
    assert cp.places == 1  # never re-placed on a different Worker
    assert uow.servers.by_id[server.id].assigned_worker_id == _WORKER
    # Both starts targeted the same Worker.
    assert [w for k, w, _ in cp.dispatched if k == "start"] == [_WORKER, _WORKER]


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


async def test_crash_loop_start_backs_off_despite_successful_dispatch() -> None:
    # A boot-crash server (#343): desired=running, observed=crashed, every start
    # dispatch SUCCEEDS (the Worker launches the container) but the MC process dies
    # again, so the row stays reconcilable across ticks. The successful dispatch
    # must NOT clear the backoff; consecutive crash restarts must space out
    # exponentially instead of re-hydrating + re-starting at full cadence forever.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)

    await reconciler.tick()  # start dispatched (hydrate+start), crash counted -> 30s
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]
    assert reconciler._attempts[server.id].failures == 1

    clock.set(_NOW + dt.timedelta(seconds=10))
    await reconciler.tick()  # within backoff -> not re-dispatched
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]

    clock.set(_NOW + dt.timedelta(seconds=40))
    await reconciler.tick()  # past 30s backoff -> retried, crash counted -> 60s
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start", "hydrate", "start"]
    assert reconciler._attempts[server.id].failures == 2

    # The grown window now spaces the next retry out: 31s after the second attempt
    # is still inside the 60s window.
    clock.set(_NOW + dt.timedelta(seconds=40 + 31))
    await reconciler.tick()
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start", "hydrate", "start"]


async def test_crash_loop_flapping_through_starting_keeps_backoff_growing() -> None:
    # Regression for the flap (#343 review): a boot-crash server cycles
    # observed crashed -> starting -> crashed. While observed=starting it drops out
    # of list_reconcilable. The backoff entry must NOT be erased during that absent
    # window (the failure count must keep GROWING across cycles), otherwise damping
    # collapses to ~one base step forever.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)
    stored = uow.servers.by_id[server.id]

    def crash_at(when: dt.datetime) -> None:
        # The server re-arrives crashed, reported far enough in the past to be out
        # of the grace window at `when`.
        stored.observed_state = ObservedState.CRASHED
        stored.observed_at = when - dt.timedelta(seconds=_GRACE + 1)

    # Cycle 1: crashed -> dispatch -> crash counted (30s backoff).
    await reconciler.tick()
    assert reconciler._attempts[server.id].failures == 1

    # The dispatched start moves it to starting: now ABSENT from list_reconcilable.
    stored.observed_state = ObservedState.STARTING
    clock.set(_NOW + dt.timedelta(seconds=15))
    await reconciler.tick()  # absent tick: entry must survive, count unchanged
    assert reconciler._attempts[server.id].failures == 1

    # Cycle 2: crashes again, past the 30s window -> retried, crash counted (60s).
    t2 = _NOW + dt.timedelta(seconds=40)
    crash_at(t2)
    clock.set(t2)
    await reconciler.tick()
    assert reconciler._attempts[server.id].failures == 2

    # Flap through starting again.
    stored.observed_state = ObservedState.STARTING
    clock.set(t2 + dt.timedelta(seconds=15))
    await reconciler.tick()  # absent tick: still survives, count unchanged
    assert reconciler._attempts[server.id].failures == 2

    # Cycle 3: crashes again, past the 60s window -> retried, crash counted (120s).
    t3 = t2 + dt.timedelta(seconds=70)
    crash_at(t3)
    clock.set(t3)
    await reconciler.tick()
    assert reconciler._attempts[server.id].failures == 3
    # Backoff grew 30s -> 60s -> 120s across cycles despite the intervening absences.
    assert reconciler._attempts[server.id].next_eligible_at == t3 + dt.timedelta(
        seconds=120
    )


async def test_healed_server_backoff_entry_expires() -> None:
    # A server that crash-looped then genuinely healed (stays observed=running, so
    # absent from list_reconcilable) must have its backoff entry dropped once enough
    # time has lapsed, so the in-memory map does not grow without bound.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)

    await reconciler.tick()  # crash counted -> entry created (30s backoff)
    entry = reconciler._attempts[server.id]
    assert entry.failures == 1

    # It heals: observed=running, absent from list_reconcilable from now on.
    uow.servers.by_id[server.id].observed_state = ObservedState.RUNNING

    # Just before next_eligible_at + backoff_max slack: entry still retained.
    expiry = entry.next_eligible_at + dt.timedelta(seconds=3600)
    clock.set(expiry - dt.timedelta(seconds=1))
    await reconciler.tick()
    assert server.id in reconciler._attempts

    # At/after the expiry instant: entry is dropped.
    clock.set(expiry)
    await reconciler.tick()
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


async def test_invalid_state_convergence_stops_reselecting_the_row() -> None:
    # The API-restart self-heal (issue #213): desired=running, assigned,
    # observed=unknown (Register carries no instance list, steady-state instances
    # emit no events). The first post-grace tick redispatches start; the live
    # Worker rejects the launch with INVALID_STATE, which redispatch_start records
    # as observed=running. That convergence must take the row out of the
    # reconcilable set so the NEXT tick dispatches nothing -- without the observed
    # write no StatusChange ever arrives and the reconciler hot-loops forever.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcomes={
            "start": CommandOutcome(
                status=CommandStatus.INVALID_STATE, message="running"
            )
        }
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)
    await reconciler.tick()
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]
    # The row converged to observed=running and is no longer reconcilable.
    assert uow.servers.by_id[server.id].observed_state is ObservedState.RUNNING
    assert await uow.servers.list_reconcilable() == []
    # A subsequent tick re-selects nothing and dispatches nothing.
    cp.dispatched.clear()
    await reconciler.tick()
    assert cp.dispatched == []


async def test_invalid_state_convergence_from_crashed_does_not_back_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Re-review regression (#343): desired=running, assigned, observed=CRASHED past
    # grace. The redispatch succeeds via INVALID_STATE -- the Worker is already
    # running the server and rejects the double-start, so redispatch_start records
    # observed=running on its freshly-loaded entity. That server GENUINELY converged
    # to running; it must NOT be miscounted as a crash. The post-dispatch crash check
    # must read the entity the lifecycle returns (observed=running), not the stale
    # list_reconcilable snapshot (still CRASHED) -- otherwise a spurious _record_failure
    # fires and a false "crash-looping ... backing off" WARN is logged.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcomes={
            "start": CommandOutcome(
                status=CommandStatus.INVALID_STATE, message="running"
            )
        }
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)

    with caplog.at_level(logging.WARNING):
        await reconciler.tick()

    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]
    # The convergence recorded observed=running; no backoff entry was created.
    assert uow.servers.by_id[server.id].observed_state is ObservedState.RUNNING
    assert server.id not in reconciler._attempts
    # No spurious crash-looping warning.
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
