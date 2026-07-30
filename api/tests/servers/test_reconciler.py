"""Use-case tests for the desired/observed divergence reconciler (issue #101).

Drives :class:`RunReconcilerTick` against in-memory fakes with a faked clock over
the divergence matrix: each actionable case re-dispatches the right command, the
non-actionable cases are skipped, the grace window suppresses a fresh divergence,
per-server exponential backoff prevents thrash within its window, and a divergence
on a disconnected Worker is skipped (its observed=unknown is expected; the
reconnect rebuild owns it).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid

import pytest

from mc_server_dashboard_api.fleet.adapters.grpc_server import _STATE_BY_PROTO
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.domain.control_plane import (
    Command as FleetCommand,
)
from mc_server_dashboard_api.fleet.domain.control_plane import (
    CommandResult,
    CommandResultCode,
    HydrateCommand,
    StartServerCommand,
)
from mc_server_dashboard_api.fleet.domain.control_plane import (
    ControlPlane as FleetControlPlane,
)
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId as FleetWorkerId
from mc_server_dashboard_api.servers.adapters.control_plane import (
    FleetControlPlaneAdapter,
)
from mc_server_dashboard_api.servers.application.lifecycle import (
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.application.reconciler import RunReconcilerTick
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
    ControlPlane,
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from tests.fleet.fakes import FakeClock as FleetFakeClock
from tests.fleet.fakes import make_worker
from tests.servers.fakes import (
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeServerRepository,
    FakeStoreGenerationReader,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_WORKER = WorkerId(uuid.uuid4())
_GRACE = 60
# The short held-start grace (issue #999): well below the full grace so a test can
# pin a divergence age BETWEEN them and prove which grace was applied.
_HELD_GRACE = 10
_PAST_GRACE = _NOW - dt.timedelta(seconds=_GRACE + 1)


def _server(
    *,
    desired: DesiredState,
    observed: ObservedState,
    worker: WorkerId | None,
    observed_at: dt.datetime | None = _PAST_GRACE,
    updated_at: dt.datetime = _PAST_GRACE,
) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=desired,
        observed_state=observed,
        observed_at=observed_at,
        assigned_worker_id=worker,
        created_at=_PAST_GRACE,
        updated_at=updated_at,
    )


class _CapturingFleet(FleetControlPlane):
    """A fleet control plane that answers every dispatch OK and records the kinds.

    Lets the #2477 test drive the REAL :class:`FleetControlPlaneAdapter` over a real
    registry — so the held-working-set inventory is the production one — while the
    hydrate/start round trips succeed without a live Worker.
    """

    def __init__(self) -> None:
        self.kinds: list[str] = []

    async def dispatch(
        self,
        *,
        worker_id: FleetWorkerId,
        server_id: str,
        command: FleetCommand,
        timeout_override: float | None = None,
        snapshot_is_final: bool = False,
    ) -> CommandResult:
        if isinstance(command, HydrateCommand):
            self.kinds.append("hydrate")
        elif isinstance(command, StartServerCommand):
            self.kinds.append("start")
        return CommandResult(code=CommandResultCode.OK)


def _reconciler(
    uow: FakeUnitOfWork,
    cp: ControlPlane,
    clock: FakeClock,
    *,
    store_generation: int = 0,
) -> RunReconcilerTick:
    return RunReconcilerTick(
        uow=uow,
        make_start_server=lambda: StartServer(
            uow=uow,
            control_plane=cp,
            clock=clock,
            jar_provisioner=FakeJarProvisioner(),
            store_generation=FakeStoreGenerationReader(generation=store_generation),
            file_store=FakeFileStore(seed_eula=True),
        ),
        make_stop_server=lambda: StopServer(uow=uow, control_plane=cp, clock=clock),
        control_plane=cp,
        store_generation=FakeStoreGenerationReader(generation=store_generation),
        clock=clock,
        grace_seconds=_GRACE,
        held_start_grace_seconds=_HELD_GRACE,
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
    # The confirmed stop takes the same post-stop final snapshot as a direct
    # server:stop (issue #846, FR-DATA-7).
    assert [k for k, _, _ in cp.dispatched] == ["stop", "snapshot"]
    assert cp.decremented == []  # the original stop owns the decrement


async def test_wedged_stopped_stopped_assigned_clears_assignment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Issue #847 (bug 2), issue #1004: a stop whose final-snapshot window was
    # interrupted by an API crash or an HTTP-task cancellation leaves the row wedged
    # at (desired=stopped, observed=stopped, assigned) — the deferred unassign never
    # ran. The reconciler's stale-stop arm re-drives the final snapshot (when the
    # worker is connected) then clears the assignment so a later start can re-place
    # with the correct store generation.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.STOPPED,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    with caplog.at_level(logging.INFO):
        await _reconciler(uow, cp, clock).tick()
    # Issue #1004: the final snapshot is re-driven before clearing.
    assert [k for k, _, _ in cp.dispatched] == ["snapshot"]
    assert uow.servers.by_id[server.id].assigned_worker_id is None
    assert any(
        "recovered" in record.message.lower() or "wedge" in record.message.lower()
        for record in caplog.records
    )


async def test_wedged_stopped_stopped_assigned_within_grace_is_skipped() -> None:
    # The stale-stop recovery only fires PAST grace: a final snapshot legitimately
    # in flight holds (stopped, stopped, assigned) for the snapshot's duration, and
    # the grace window keeps the reconciler from yanking the assignment out from
    # under an in-progress snapshot.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.STOPPED,
        worker=_WORKER,
        observed_at=_NOW - dt.timedelta(seconds=_GRACE - 1),
        updated_at=_NOW - dt.timedelta(seconds=_GRACE - 1),
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert cp.dispatched == []
    assert uow.servers.by_id[server.id].assigned_worker_id == _WORKER


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


async def test_fresh_intent_on_stale_observed_is_within_grace() -> None:
    # A fresh start commits desired=running + updated_at=now on a server last
    # observed stopped long ago (observed_at past grace). update_lifecycle
    # refreshes updated_at but NOT observed_at, so grace measured from
    # observed_at alone would give zero grace and re-dispatch start concurrently
    # with the in-flight HTTP start. Grace is measured from updated_at too (#774).
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.STOPPED,
        worker=_WORKER,
        observed_at=_PAST_GRACE,
        updated_at=_NOW - dt.timedelta(seconds=_GRACE - 1),
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


# --- held-aware short start grace (issue #999) -----------------------------


async def test_held_redispatch_start_acts_after_short_grace_not_full_grace() -> None:
    # desired=running, assigned, observed crashed, worker connected AND holding a
    # working set at least as fresh as the store (held=store=2): the start will SKIP
    # hydrate, so the SHORT held-start grace applies. A divergence aged between the
    # short and full grace is acted on now — and would NOT have been under the old
    # uniform full grace.
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_HELD_GRACE + 1)
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(held={(_WORKER, server.id): 2})
    clock = FakeClock(_NOW)
    # Sanity: the divergence is still WITHIN the full grace, so the old uniform
    # grace would not have acted yet.
    assert (_NOW - aged) < dt.timedelta(seconds=_GRACE)
    await _reconciler(uow, cp, clock, store_generation=2).tick()
    assert [k for k, _, _ in cp.dispatched] == ["start"]  # command-only, no hydrate


async def test_held_redispatch_start_still_waits_within_short_grace() -> None:
    # The held short grace still suppresses a fresh divergence inside its own window:
    # a held start aged below held_start_grace_seconds is not acted on yet.
    uow = FakeUnitOfWork()
    fresh = _NOW - dt.timedelta(seconds=_HELD_GRACE - 1)
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
        observed_at=fresh,
        updated_at=fresh,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(held={(_WORKER, server.id): 2})
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock, store_generation=2).tick()
    assert cp.dispatched == []


async def test_not_held_redispatch_start_still_waits_full_grace() -> None:
    # Worker connected but does NOT hold the working set (held absent -> None): the
    # start will hydrate, so the FULL grace still applies. A divergence aged past the
    # short grace but within the full grace is NOT acted on (no regression of the
    # #822 duplicate-start floor on the hydrate path).
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_HELD_GRACE + 1)
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()  # nothing held -> hydrate -> full grace
    clock = FakeClock(_NOW)
    assert (_NOW - aged) < dt.timedelta(seconds=_GRACE)
    await _reconciler(uow, cp, clock).tick()
    assert cp.dispatched == []


async def test_stale_held_redispatch_start_still_waits_full_grace() -> None:
    # Worker holds a STALE generation (held=1 < store=2): the start MUST hydrate, so
    # the full grace still applies — the same held >= store predicate the lifecycle
    # skip-hydrate uses (#763). Aged past the short grace but within the full grace:
    # not acted on.
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_HELD_GRACE + 1)
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(held={(_WORKER, server.id): 1})
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock, store_generation=2).tick()
    assert cp.dispatched == []


async def test_orphan_place_and_start_uses_full_grace_despite_held() -> None:
    # place_and_start (no assigned worker) always hydrates and may place on a
    # DIFFERENT worker, so the full grace (the #822 cross-worker floor) always
    # applies — even though a (now-stale) held entry exists for some worker. Aged
    # past the short grace but within the full grace: not acted on.
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_HELD_GRACE + 1)
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker=None,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(place_to=_WORKER, held={(_WORKER, server.id): 2})
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock, store_generation=2).tick()
    assert cp.dispatched == []


async def test_redispatch_stop_uses_full_grace_despite_held() -> None:
    # The stop side (redispatch_stop) keeps the full grace (the #847 stale-snapshot
    # floor): even with a held entry, a stop divergence aged past the short grace but
    # within the full grace is NOT acted on.
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_HELD_GRACE + 1)
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(held={(_WORKER, server.id): 2})
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock, store_generation=2).tick()
    assert cp.dispatched == []


async def test_clear_stale_assignment_uses_full_grace_despite_held() -> None:
    # The wedged-stop recovery (clear_stale_assignment) keeps the full grace (#847):
    # a held entry must not shorten the window that protects an in-flight final
    # snapshot. Aged past the short grace but within the full grace: assignment kept.
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_HELD_GRACE + 1)
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.STOPPED,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(held={(_WORKER, server.id): 2})
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock, store_generation=2).tick()
    assert cp.dispatched == []
    assert uow.servers.by_id[server.id].assigned_worker_id == _WORKER


async def test_server_placed_after_registration_gets_the_short_held_grace() -> None:
    # Issue #2477: the held inventory must reflect what the Worker holds NOW, not
    # only what it advertised at registration. Register the Worker with an EMPTY
    # held inventory -- the state of every server placed SINCE that registration --
    # then let a real start hydrate the working set onto it. The Worker now
    # demonstrably holds the hydrated generation, so a later divergence aged between
    # the short and the full grace must be acted on under held_start_grace_seconds
    # (command-only re-dispatch), not left waiting the full hydrate-based grace.
    fleet = _CapturingFleet()
    registry = InMemoryWorkerRegistry(
        clock=FleetFakeClock(_NOW), heartbeat_timeout=dt.timedelta(seconds=30)
    )
    registry.register(make_worker(worker_id=str(_WORKER.value), at=_NOW))
    cp = FleetControlPlaneAdapter(
        registry=registry,
        control_plane=fleet,
        data_plane_base_url="http://data-plane.test",
        worker_credential="test-credential",
    )
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_HELD_GRACE + 1)
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    clock = FakeClock(_NOW)
    # The start that placed this server on the Worker: it hydrates, because nothing
    # was held for an id that did not exist at registration time.
    await StartServer(
        uow=uow,
        control_plane=cp,
        clock=clock,
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(generation=2),
        file_store=FakeFileStore(seed_eula=True),
    ).redispatch_start(community_id=server.community_id, server_id=server.id)
    assert fleet.kinds == ["hydrate", "start"]
    # Sanity: the divergence is still WITHIN the full grace, so only the short
    # held-start grace can act here.
    assert (_NOW - aged) < dt.timedelta(seconds=_GRACE)

    await _reconciler(uow, cp, clock, store_generation=2).tick()

    # A second, command-only start: the short grace applied AND the re-dispatch
    # skipped the destructive hydrate, the same held >= store predicate (#763).
    assert fleet.kinds == ["hydrate", "start", "start"]


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
    # One redispatch_stop action: the stop plus its post-stop final snapshot (#846).
    assert [k for k, _, _ in cp.dispatched] == ["stop", "snapshot"]
    assert server.id not in reconciler._attempts


async def test_crash_loop_start_backs_off_despite_successful_dispatch() -> None:
    # A boot-crash server (#343): desired=running, observed=crashed, every start
    # dispatch SUCCEEDS (the Worker launches the container) but the MC process dies
    # again, so the row stays reconcilable across ticks. The successful dispatch
    # must NOT clear the backoff; consecutive crash restarts must space out
    # exponentially instead of re-starting at full cadence forever.
    #
    # The dispatch shape changed deliberately in #2442/#2477: the FIRST attempt's
    # hydrate refreshes the held-working-set inventory, so every later retry sees the
    # Worker holding a fresh working set and is command-only. Only the first attempt
    # hydrates now. The subject here is the backoff, and it is unchanged.
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
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start", "start"]
    assert reconciler._attempts[server.id].failures == 2

    # The grown window now spaces the next retry out: 31s after the second attempt
    # is still inside the 60s window.
    clock.set(_NOW + dt.timedelta(seconds=40 + 31))
    await reconciler.tick()
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start", "start"]


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


# --- concurrent dispatch (#871) --------------------------------------------


class _GatedControlPlane(FakeControlPlane):
    """Control plane whose first dispatch per server blocks on a release event.

    A test sets up N servers, ticks, and waits until every server's first
    dispatch has STARTED (``started``) before releasing them (``release``). If the
    tick processed servers serially, only one dispatch could be in flight at a
    time and ``started`` would never reach N — the wait would time out. Reaching N
    in-flight dispatches is the proof the actions ran concurrently.
    """

    def __init__(self, *, expected: int) -> None:
        super().__init__()
        self._release = asyncio.Event()
        self._in_flight = 0
        self._all_started = asyncio.Event()
        self._expected = expected

    async def _record(
        self, kind: str, worker_id: WorkerId, server_id: ServerId
    ) -> CommandOutcome:
        self._in_flight += 1
        if self._in_flight >= self._expected:
            self._all_started.set()
        await self._release.wait()
        return await super()._record(kind, worker_id, server_id)

    async def wait_all_started(self) -> None:
        await asyncio.wait_for(self._all_started.wait(), timeout=2.0)

    def release(self) -> None:
        self._release.set()


def _concurrent_reconciler(
    repo: FakeServerRepository, cp: FakeControlPlane, clock: FakeClock
) -> RunReconcilerTick:
    # Production gives every action its OWN UnitOfWork (#871); mirror that here by
    # minting a fresh FakeUnitOfWork over the SHARED repository per factory call,
    # the way each production UoW shares the one database.
    def fresh_uow() -> FakeUnitOfWork:
        return FakeUnitOfWork(servers=repo)

    return RunReconcilerTick(
        uow=FakeUnitOfWork(servers=repo),
        make_start_server=lambda: StartServer(
            uow=fresh_uow(),
            control_plane=cp,
            clock=clock,
            jar_provisioner=FakeJarProvisioner(),
            store_generation=FakeStoreGenerationReader(),
            file_store=FakeFileStore(seed_eula=True),
        ),
        make_stop_server=lambda: StopServer(
            uow=fresh_uow(), control_plane=cp, clock=clock
        ),
        control_plane=cp,
        store_generation=FakeStoreGenerationReader(),
        clock=clock,
        grace_seconds=_GRACE,
        held_start_grace_seconds=_HELD_GRACE,
        backoff_base_seconds=30,
        backoff_max_seconds=3600,
    )


async def test_slow_actions_for_different_servers_run_concurrently() -> None:
    # Two servers each owe a redispatch_stop; the control plane blocks every
    # dispatch until BOTH have started. A serial tick could only ever have one
    # dispatch in flight, so wait_all_started would time out. Reaching two
    # in-flight dispatches proves the per-server actions ran concurrently (#871).
    repo = FakeServerRepository()
    for _ in range(2):
        repo.seed(
            _server(
                desired=DesiredState.STOPPED,
                observed=ObservedState.RUNNING,
                worker=_WORKER,
            )
        )
    cp = _GatedControlPlane(expected=2)
    clock = FakeClock(_NOW)
    reconciler = _concurrent_reconciler(repo, cp, clock)

    tick = asyncio.ensure_future(reconciler.tick())
    await cp.wait_all_started()  # both dispatches in flight at once -> concurrent
    cp.release()
    await asyncio.wait_for(tick, timeout=2.0)

    # Each server completed its stop (+ post-stop final snapshot, #846).
    assert sorted(k for k, _, _ in cp.dispatched) == [
        "snapshot",
        "snapshot",
        "stop",
        "stop",
    ]


async def test_concurrency_is_capped() -> None:
    # With the cap at 4, a fifth simultaneous action must wait for a slot: only 4
    # dispatches are in flight while the gate is held. Five servers, expected=5
    # would deadlock-then-time-out at the gate, proving the cap holds.
    repo = FakeServerRepository()
    for _ in range(5):
        repo.seed(
            _server(
                desired=DesiredState.STOPPED,
                observed=ObservedState.RUNNING,
                worker=_WORKER,
            )
        )
    cp = _GatedControlPlane(expected=5)
    clock = FakeClock(_NOW)
    reconciler = _concurrent_reconciler(repo, cp, clock)

    tick = asyncio.ensure_future(reconciler.tick())
    with pytest.raises(asyncio.TimeoutError):
        await cp.wait_all_started()  # never reaches 5 in flight: capped at 4
    assert cp._in_flight == 4
    cp.release()
    await asyncio.wait_for(tick, timeout=2.0)


async def test_failure_in_one_action_does_not_poison_others() -> None:
    # One server's start use case is wired with a UoW that raises mid-action; the
    # other server's action uses an independent, healthy UoW. The failure must be
    # contained: the healthy server converges and clears its backoff, the failing
    # server backs off — neither leaks into the other (#871 failure isolation).
    repo = FakeServerRepository()
    healthy = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.STOPPED,
        worker=_WORKER,
    )
    poisoned = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.STOPPED,
        worker=_WORKER,
    )
    repo.seed(healthy)
    repo.seed(poisoned)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)

    class _BoomUnitOfWork(FakeUnitOfWork):
        async def __aenter__(self) -> "_BoomUnitOfWork":
            raise RuntimeError("transaction poisoned")

    def make_start() -> StartServer:
        # The first factory call (healthy server, seeded first) gets the
        # exploding UoW; every later call gets a healthy one.
        uow = (
            _BoomUnitOfWork(servers=repo) if not made else FakeUnitOfWork(servers=repo)
        )
        made.append(True)
        return StartServer(
            uow=uow,
            control_plane=cp,
            clock=clock,
            jar_provisioner=FakeJarProvisioner(),
            store_generation=FakeStoreGenerationReader(),
            file_store=FakeFileStore(seed_eula=True),
        )

    made: list[bool] = []
    reconciler = RunReconcilerTick(
        uow=FakeUnitOfWork(servers=repo),
        make_start_server=make_start,
        make_stop_server=lambda: StopServer(
            uow=FakeUnitOfWork(servers=repo), control_plane=cp, clock=clock
        ),
        control_plane=cp,
        store_generation=FakeStoreGenerationReader(),
        clock=clock,
        grace_seconds=_GRACE,
        held_start_grace_seconds=_HELD_GRACE,
        backoff_base_seconds=30,
        backoff_max_seconds=3600,
    )

    await reconciler.tick()

    # Exactly one server backed off (the poisoned one); the other did not.
    assert len(reconciler._attempts) == 1
    backed_off = next(iter(reconciler._attempts))
    healed = healthy.id if backed_off == poisoned.id else poisoned.id
    assert healed not in reconciler._attempts
    # The healthy server actually converged (its start was dispatched).
    assert any(sid == healed for _, _, sid in cp.dispatched)


async def test_backoff_remains_per_server_under_concurrency() -> None:
    # Two servers fail their actions in the same concurrent tick; each must get its
    # OWN backoff entry at failures==1 — the in-memory map stays per-server correct
    # despite the actions interleaving at awaits (#871).
    repo = FakeServerRepository()
    a = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
    )
    b = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
    )
    repo.seed(a)
    repo.seed(b)
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.INTERNAL, message="boom")
    )
    clock = FakeClock(_NOW)
    reconciler = _concurrent_reconciler(repo, cp, clock)

    await reconciler.tick()

    assert set(reconciler._attempts) == {a.id, b.id}
    assert reconciler._attempts[a.id].failures == 1
    assert reconciler._attempts[b.id].failures == 1


async def test_exception_escaping_consider_is_logged_and_tick_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An exception that escapes _consider's own error handling (e.g. a bug in
    # _within_grace or _action_for) must be logged at ERROR level (NFR-OBS-1)
    # and must not prevent the other server's action from running.
    repo = FakeServerRepository()
    good = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.STOPPED,
        worker=_WORKER,
    )
    bad = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.STOPPED,
        worker=_WORKER,
    )
    repo.seed(good)
    repo.seed(bad)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)

    reconciler = _concurrent_reconciler(repo, cp, clock)

    # Patch _consider to raise unconditionally for one specific server id.
    original_consider = reconciler._consider

    async def _patched_consider(server: Server, now: dt.datetime) -> None:
        if server.id == bad.id:
            raise RuntimeError("_consider blew up unexpectedly")
        await original_consider(server, now)

    reconciler._consider = _patched_consider  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="mc_server_dashboard_api"):
        await reconciler.tick()

    # The good server's action still ran despite the exception on the bad one.
    assert any(sid == good.id for _, _, sid in cp.dispatched)
    # The exception was logged.
    assert any(
        "unhandled exception" in record.message and record.levelno >= logging.ERROR
        for record in caplog.records
    )


# --- stopped/unknown/assigned wedge recovery (issue #1599) -----------------


async def test_stopped_unknown_assigned_connected_redispatches_stop() -> None:
    # Issue #1599: (desired=stopped, observed=unknown, assigned) with the worker
    # still connected -> redispatch the stop to converge.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.UNKNOWN,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    # A confirmed stop dispatches stop + post-stop final snapshot.
    assert [k for k, _, _ in cp.dispatched] == ["stop", "snapshot"]
    assert cp.decremented == []  # the original stop owns the decrement


async def test_stopped_unknown_assigned_connected_server_not_found() -> None:
    # Issue #1599: worker connected + SERVER_NOT_FOUND (the process already exited)
    # -> converges to stopped and unassigns.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.UNKNOWN,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcomes={"stop": CommandOutcome(status=CommandStatus.SERVER_NOT_FOUND)}
    )
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert [k for k, _, _ in cp.dispatched] == ["stop"]
    stored = uow.servers.by_id[server.id]
    assert stored.assigned_worker_id is None
    assert stored.observed_state is ObservedState.STOPPED


async def test_stopped_unknown_assigned_disconnected_clears_assignment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Issue #1599: (desired=stopped, observed=unknown, assigned) with the worker
    # DISCONNECTED -> DB-only clear (no command dispatched), with WARNING log.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.UNKNOWN,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(connected={_WORKER: False})
    clock = FakeClock(_NOW)
    with caplog.at_level(logging.WARNING):
        await _reconciler(uow, cp, clock).tick()
    assert cp.dispatched == []
    assert uow.servers.by_id[server.id].assigned_worker_id is None
    assert any(
        "1599" in record.message or "unknown" in record.message.lower()
        for record in caplog.records
        if record.levelno == logging.WARNING
    )


async def test_stopped_unknown_assigned_within_grace_is_skipped() -> None:
    # The wedge recovery only fires past grace, preserving the in-flight window.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.UNKNOWN,
        worker=_WORKER,
        observed_at=_NOW - dt.timedelta(seconds=_GRACE - 1),
        updated_at=_NOW - dt.timedelta(seconds=_GRACE - 1),
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert cp.dispatched == []
    assert uow.servers.by_id[server.id].assigned_worker_id == _WORKER


# --- stopped/crashed/assigned wedge recovery (issue #2439) -----------------


async def test_stopped_crashed_assigned_connected_snapshots_then_clears(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Issue #2439: a stop whose dispatch failed left (stopped, running, assigned)
    # and the process then died on its own, so the worker reported crashed. The
    # process is gone, so the owed work is the release half of the stop path: take
    # the final snapshot the crash never got (the worker retains the scratch), then
    # release the assignment. No second stop dispatch — the worker has already
    # forgotten the crashed instance, so a stop would answer SERVER_NOT_FOUND and
    # skip the snapshot, losing the crash-window world.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    with caplog.at_level(logging.INFO):
        await _reconciler(uow, cp, clock).tick()
    assert [k for k, _, _ in cp.dispatched] == ["snapshot"]
    assert uow.servers.by_id[server.id].assigned_worker_id is None
    assert any("recovered" in record.message.lower() for record in caplog.records)


async def test_stopped_crashed_assigned_disconnected_clears_without_snapshot() -> None:
    # Issue #2439: the same wedge with the worker GONE — the snapshot cannot run,
    # but the assignment must still be released (DB-only), exactly as the
    # (stopped, stopped, assigned) arm does.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(connected={_WORKER: False})
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert cp.dispatched == []
    assert uow.servers.by_id[server.id].assigned_worker_id is None


async def test_stopped_crashed_assigned_within_grace_is_skipped() -> None:
    # Issue #2439: the recovery only fires past grace, like every other stop-side
    # arm — a crash report arriving while a stop is still settling must not have
    # its assignment yanked out from under the in-flight path.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
        observed_at=_NOW - dt.timedelta(seconds=_GRACE - 1),
        updated_at=_NOW - dt.timedelta(seconds=_GRACE - 1),
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert cp.dispatched == []
    assert uow.servers.by_id[server.id].assigned_worker_id == _WORKER


async def test_stopped_crashed_clear_is_not_counted_as_a_crash_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Issue #2439: the post-dispatch crash check exists to damp a boot-crash LOOP
    # under a start intent (#343). The stop-side release leaves observed=crashed
    # standing (it is the truth: the process died), so that check must not fire
    # here — it would log a false "crash-looping" WARN and arm a backoff for an
    # action that fully succeeded.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)
    with caplog.at_level(logging.WARNING):
        await reconciler.tick()
    assert uow.servers.by_id[server.id].assigned_worker_id is None
    assert server.id not in reconciler._attempts
    assert not [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "crash-looping" in r.getMessage()
    ]


# --- stopped/stopping/assigned wedge recovery (issue #2452) ----------------


async def test_stopped_stopping_assigned_connected_redispatches_stop() -> None:
    # Issue #2452: a stop whose dispatch failed after the worker emitted stopping
    # leaves (stopped, stopping, assigned) and NO terminal report ever follows —
    # both of the worker's Stop failure paths restore the pre-stop state without
    # emitting. With the worker connected the owed work is a real retry of the
    # stop, which is also what retires the worker-side orphan record.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.STOPPING,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert [k for k, _, _ in cp.dispatched] == ["stop", "snapshot"]
    stored = uow.servers.by_id[server.id]
    assert stored.observed_state is ObservedState.STOPPED
    assert stored.assigned_worker_id is None


async def test_stopped_stopping_assigned_disconnected_clears_assignment() -> None:
    # Issue #2452: the same wedge with the worker GONE — there is nobody to retry
    # the stop against, so release the assignment (DB-only) exactly as the
    # observed=unknown arm does, converging observed to unknown so the released
    # row is at rest rather than stranded at (stopped, stopping, unassigned).
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.STOPPING,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(connected={_WORKER: False})
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert cp.dispatched == []
    stored = uow.servers.by_id[server.id]
    assert stored.assigned_worker_id is None
    assert stored.observed_state is ObservedState.UNKNOWN
    assert stored.is_at_rest()
    # The released row is settled: no arm claims it again on a later tick.
    assert await uow.servers.list_reconcilable() == []


async def test_stopped_starting_assigned_is_not_reconcilable() -> None:
    # Issue #2452: starting stays excluded from the stopped-intent arms. Unlike
    # stopping, it is a genuine transient — the worker's launch settles into a
    # terminal state whose report moves the row onto the (stopped, running) or
    # (stopped, crashed) arm — so acting on it would race the in-flight launch.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.STARTING,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert cp.dispatched == []
    assert uow.servers.by_id[server.id].assigned_worker_id == _WORKER


# --- Worker-reported unknown lands on existing arms (issue #2474) -----------
#
# SERVER_STATE_UNKNOWN joined the wire enum, so ``unknown`` now reaches a row from a
# Worker's StatusChange and not only from the API's own disconnect inference. Three
# wedges of the "no arm matches this shape" kind were fixed in this cycle (#1599,
# #2439, #2452), so the landing is pinned rather than assumed. These tests derive the
# observed state from the INGEST MAPPING itself, so they fail if that mapping ever
# routes the wire value onto a different state.

_WORKER_REPORTED_UNKNOWN = ObservedState(_STATE_BY_PROTO[pb.SERVER_STATE_UNKNOWN])


async def test_worker_reported_unknown_under_running_intent_redispatches_start() -> (
    None
):
    # desired=running, assigned, worker connected -> UNKNOWN is in _NOT_RUNNING, so
    # the stale-start arm claims it and re-dispatches (hydrate + start).
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=_WORKER_REPORTED_UNKNOWN,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]


async def test_worker_reported_unknown_under_stopped_intent_redispatches_stop() -> None:
    # desired=stopped, assigned, worker connected -> the #1599 arm claims it and
    # retries the stop (then the post-stop final snapshot).
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=_WORKER_REPORTED_UNKNOWN,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert [k for k, _, _ in cp.dispatched] == ["stop", "snapshot"]
    assert uow.servers.by_id[server.id].assigned_worker_id is None


async def test_worker_reported_unknown_unassigned_is_at_rest_not_wedged() -> None:
    # The terminal shape: nothing left to release and no intent to replay, so every
    # stopped-intent arm (each requires an assignment) correctly passes. It must be
    # SETTLED rather than inert — at rest, so a later start can re-place it.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=_WORKER_REPORTED_UNKNOWN,
        worker=None,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert cp.dispatched == []
    assert await uow.servers.list_reconcilable() == []
    assert uow.servers.by_id[server.id].is_at_rest()
