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
from mc_server_dashboard_api.servers.application.stop_dispatch_refusals import (
    StopDispatchRefusals,
)
from mc_server_dashboard_api.servers.application.unrecorded_stops import (
    UnrecordedStops,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
    ControlPlane,
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    CommandDispatchError,
    InvalidLifecycleTransitionError,
)
from mc_server_dashboard_api.servers.domain.store_generation import (
    StoreGenerationReader,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    CrashReason,
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
from tests.servers.contract_table import worker_status
from tests.servers.fakes import (
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeServerRepository,
    FakeStoreGenerationReader,
    FakeUnitOfWork,
    PlacementSequenceControlPlane,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_WORKER = WorkerId(uuid.uuid4())
_GRACE = 60
# The short held-start grace (issue #999): well below the full grace so a test can
# pin a divergence age BETWEEN them and prove which grace was applied.
_HELD_GRACE = 10
# The short refused-stop grace (issue #2478): below both of the above so a test can
# pin a divergence age between it and the full grace and prove it was applied.
_REFUSED_GRACE = 5
# The restart budget (issue #3209): ABOVE the full grace so a test can pin a
# divergence age between them and prove the budget, not a grace, held the start back.
_RESTART_BUDGET = 120
_PAST_GRACE = _NOW - dt.timedelta(seconds=_GRACE + 1)
# This process's control-plane listener started well before every other instant
# here, unless a test says otherwise (issue #3916).
_STARTED_LONG_AGO = _NOW - dt.timedelta(days=1)


def _server(
    *,
    desired: DesiredState,
    observed: ObservedState,
    worker: WorkerId | None,
    observed_at: dt.datetime | None = _PAST_GRACE,
    updated_at: dt.datetime = _PAST_GRACE,
    crash_reason: CrashReason | None = None,
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
        crash_reason=crash_reason,
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
    store_reader: StoreGenerationReader | None = None,
    stop_refusals: StopDispatchRefusals | None = None,
    unrecorded_stops: UnrecordedStops | None = None,
    listener_started_at: dt.datetime = _STARTED_LONG_AGO,
) -> RunReconcilerTick:
    stop_refusals = stop_refusals or StopDispatchRefusals()
    unrecorded_stops = unrecorded_stops or UnrecordedStops()
    # One shared reader across both seams so a test can pin what happens when the
    # generation the grace decision reads and the one redispatch_start reads are the
    # same underlying counter (issue #2482); the default fixed-value reader makes the
    # sharing behaviourally identical to two independent instances.
    store_reader = store_reader or FakeStoreGenerationReader(
        generation=store_generation
    )
    return RunReconcilerTick(
        uow=uow,
        make_start_server=lambda: StartServer(
            uow=uow,
            control_plane=cp,
            clock=clock,
            jar_provisioner=FakeJarProvisioner(),
            store_generation=store_reader,
            file_store=FakeFileStore(seed_eula=True),
        ),
        make_stop_server=lambda: StopServer(
            uow=uow,
            control_plane=cp,
            clock=clock,
            stop_refusals=stop_refusals,
            unrecorded_stops=unrecorded_stops,
        ),
        control_plane=cp,
        store_generation=store_reader,
        clock=clock,
        stop_refusals=stop_refusals,
        unrecorded_stops=unrecorded_stops,
        grace_seconds=_GRACE,
        held_start_grace_seconds=_HELD_GRACE,
        refused_stop_grace_seconds=_REFUSED_GRACE,
        restart_timeout_seconds=_RESTART_BUDGET,
        backoff_base_seconds=30,
        backoff_max_seconds=3600,
        listener_started_at=listener_started_at,
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


# --- in-flight restart stop leg (issue #3209) -------------------------------


async def test_restart_stop_leg_outlasting_held_grace_is_not_redispatched() -> None:
    # A restart keeps desired=running and its stop leg reports ``stopping``, so the
    # row reads (running, stopping) for as long as the flush and the docker-stop
    # escalation take — which outlasts the short held-start grace. A start sent
    # then cannot succeed: it queues behind the restart in the Worker's per-server
    # lane until the dispatch times out (or, on a new session, is refused BUSY by
    # the restart's reservation). So the reconciler stands back for the restart
    # budget instead of dispatching it.
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_HELD_GRACE + 1)
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.STOPPING,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(held={(_WORKER, server.id): 2})
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock, store_generation=2).tick()
    assert cp.dispatched == []


async def test_restart_stop_leg_is_held_back_for_the_budget_not_the_full_grace() -> (
    None
):
    # The stand-back is the restart budget itself, not whichever grace the start
    # would otherwise take: a non-held row past the FULL grace but still inside the
    # budget is left alone too (an operator may run grace below stop + command).
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_RESTART_BUDGET - 1)
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.STOPPING,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()  # nothing held -> full grace, already lapsed
    clock = FakeClock(_NOW)
    assert (_NOW - aged) > dt.timedelta(seconds=_GRACE)
    await _reconciler(uow, cp, clock).tick()
    assert cp.dispatched == []


async def test_stuck_stopping_is_redispatched_once_the_restart_budget_lapses() -> None:
    # The budget lapsing is permission to retry, not proof the Worker finished
    # (its stop runs detached, on its own deadlines): a row still reading
    # (running, stopping) then is re-dispatched exactly as before. The Worker keeps
    # that safe: its per-server lane serialises the start behind a restart still
    # running on the session, and its reservation/orphan guards refuse it BUSY.
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_RESTART_BUDGET + 1)
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.STOPPING,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(held={(_WORKER, server.id): 2})
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock, store_generation=2).tick()
    assert [k for k, _, _ in cp.dispatched] == ["start"]


@pytest.mark.parametrize(
    "observed",
    [
        ObservedState.STOPPED,
        ObservedState.RESTARTING,
        ObservedState.CRASHED,
        ObservedState.UNKNOWN,
    ],
)
async def test_restart_budget_applies_to_no_other_observed_state(
    observed: ObservedState,
) -> None:
    # Only ``stopping`` marks a stop leg in flight. Every other not-running state
    # under a running intent keeps the short held-start grace it had.
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_HELD_GRACE + 1)
    server = _server(
        desired=DesiredState.RUNNING,
        observed=observed,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(held={(_WORKER, server.id): 2})
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock, store_generation=2).tick()
    assert [k for k, _, _ in cp.dispatched] == ["start"]


class _BumpingStoreGenerationReader(StoreGenerationReader):
    """Returns ``first`` on the first read, then ``bumped`` on every read after.

    Models a store bump (a restore #873 or an at-rest edit #889 — both require a
    stopped server, exactly the interleaving issue #2482 describes) landing between
    the reconciler's grace-decision read of the generation and ``redispatch_start``'s
    own skip-hydrate read of it. A single shared instance is handed to both seams so
    the two reads see the counter advance between them.
    """

    def __init__(self, *, first: int, bumped: int) -> None:
        self._first = first
        self._bumped = bumped
        self.calls = 0

    async def current_generation(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> int:
        self.calls += 1
        return self._first if self.calls == 1 else self._bumped


async def test_held_redispatch_start_carries_grace_generation_over_a_bump() -> None:
    # TOCTOU pin (issue #2482): the reconciler grants the SHORT held-start grace on a
    # command-only start (held=2 >= store=2), then the store bumps to 3 (a restore /
    # at-rest edit) before redispatch_start makes its own skip-hydrate decision.
    # Reading the generation separately there would flip skip_hydrate off and HYDRATE
    # under the short grace — a round trip the short grace's budget does not cover.
    # The generation resolved for the grace decision must be CARRIED into the
    # dispatch, so held=2 >= store=2 still holds and no hydrate runs.
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
    reader = _BumpingStoreGenerationReader(first=2, bumped=3)
    # Within the full grace, so only the short held-start grace can license acting now.
    assert (_NOW - aged) < dt.timedelta(seconds=_GRACE)
    await _reconciler(uow, cp, clock, store_reader=reader).tick()
    # Command-only: the carried generation (2) keeps held >= store, so NO hydrate.
    assert [k for k, _, _ in cp.dispatched] == ["start"]


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


# --- short grace for a known-failed stop dispatch (issue #2478) -------------


def _refused(server: Server, at: dt.datetime) -> StopDispatchRefusals:
    refusals = StopDispatchRefusals()
    refusals.record_refusal(
        server.id, outcome=CommandOutcome(status=CommandStatus.INTERNAL), at=at
    )
    return refusals


async def test_refused_stop_dispatch_is_redispatched_on_the_short_grace() -> None:
    # The Worker RETURNED a refusal for the previous stop dispatch, so the round trip
    # the full grace budgets (stop_timeout_seconds, #930) has already ended and there
    # is nothing in flight for the retry to race. A divergence aged past the
    # refused-stop grace but well within the full grace is acted on now.
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_REFUSED_GRACE + 1)
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    # Sanity: the divergence is still WITHIN the full grace, so only the short
    # refused-stop grace can act here.
    assert (_NOW - aged) < dt.timedelta(seconds=_GRACE)
    await _reconciler(uow, cp, clock, stop_refusals=_refused(server, aged)).tick()
    assert [k for k, _, _ in cp.dispatched] == ["stop", "snapshot"]


async def test_possibly_unsent_stop_still_waits_the_full_grace() -> None:
    # The distinction this issue turns on (#2478/#2442): with NO recorded refusal the
    # row is indistinguishable from a stop committed by a process that died before it
    # could dispatch — the command may never have been sent, or may still be in
    # flight — so the #930 floor stands and the full grace applies. Identical row and
    # age to the test above; only the recorded verdict differs.
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_REFUSED_GRACE + 1)
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert cp.dispatched == []


async def test_refusal_older_than_the_last_worker_report_waits_the_full_grace() -> None:
    # The short grace rides on the refusal being the MOST RECENT thing known about
    # the row. A Worker report (or a fresh intent commit) landing after it supersedes
    # the verdict: what happened to the row since is unknown again, so the full grace
    # returns.
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_REFUSED_GRACE + 1)
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    stale = aged - dt.timedelta(seconds=1)
    await _reconciler(uow, cp, clock, stop_refusals=_refused(server, stale)).tick()
    assert cp.dispatched == []


async def test_busy_stop_refusal_does_not_shorten_the_grace() -> None:
    # BUSY is the Worker's reservation guard saying another mutating command is
    # already in flight for this id (issue #824), and after #2475/#2476 that is
    # typically its own converger working a failed-stop orphan. The dispatch settled,
    # but something else has NOT, so "nothing is in flight" — the property the short
    # grace rests on — does not hold. Keep the full grace; the converger owns that
    # cadence, not the reconciler (#2478).
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_REFUSED_GRACE + 1)
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    refusals = StopDispatchRefusals()
    refusals.record_refusal(
        server.id, outcome=CommandOutcome(status=CommandStatus.BUSY), at=aged
    )
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock, stop_refusals=refusals).tick()
    assert cp.dispatched == []


async def test_refused_stop_does_not_shorten_a_start_redispatch() -> None:
    # The recorded verdict is about a STOP dispatch only. A running-intent divergence
    # is re-dispatched hydrate-then-start and carries the #822 duplicate-start floor,
    # which no stop refusal speaks to: it keeps the full grace.
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_REFUSED_GRACE + 1)
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock, stop_refusals=_refused(server, aged)).tick()
    assert cp.dispatched == []


async def test_refused_stop_does_not_shorten_a_stale_assignment_clear() -> None:
    # clear_stale_assignment carries the #847 stale-snapshot floor — it must not yank
    # an assignment out from under a final snapshot still uploading — which a refused
    # stop dispatch says nothing about. It keeps the full grace.
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_REFUSED_GRACE + 1)
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.STOPPED,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock, stop_refusals=_refused(server, aged)).tick()
    assert cp.dispatched == []
    assert uow.servers.by_id[server.id].assigned_worker_id == _WORKER


async def test_operator_stop_refused_by_the_worker_converges_on_the_short_grace() -> (
    None
):
    # End to end over the SHARED journal (#2478, the user-visible half of #2442):
    # nothing is hand-seeded here — the verdict travels from the operator's failing
    # StopServer call to the reconciler's grace choice, which is what the wiring has
    # to get right. A server the operator asked to stop must not keep running, and
    # accepting players, for the full ~11-12 min after a refusal the API already has.
    uow = FakeUnitOfWork()
    long_running = _NOW - dt.timedelta(seconds=3600)
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
        observed_at=long_running,
        updated_at=long_running,
    )
    uow.servers.seed(server)
    refusals = StopDispatchRefusals()
    clock = FakeClock(_NOW)
    refusing = FakeControlPlane(
        outcomes={"stop": CommandOutcome(status=CommandStatus.INTERNAL, message="boom")}
    )
    with pytest.raises(CommandDispatchError):
        await StopServer(
            uow=uow, control_plane=refusing, clock=clock, stop_refusals=refusals
        )(community_id=server.community_id, server_id=server.id)
    # The stop intent stands and is not compensated (#2435): the row is now the
    # ambiguous (stopped, running, assigned) shape the reconciler owns.
    stored = uow.servers.by_id[server.id]
    assert stored.desired_state is DesiredState.STOPPED
    assert stored.observed_state is ObservedState.RUNNING
    assert stored.assigned_worker_id == _WORKER

    cp = FakeControlPlane()
    clock.set(_NOW + dt.timedelta(seconds=_REFUSED_GRACE + 1))
    await _reconciler(uow, cp, clock, stop_refusals=refusals).tick()

    assert [k for k, _, _ in cp.dispatched] == ["stop", "snapshot"]
    assert uow.servers.by_id[server.id].assigned_worker_id is None


def _stale_stop_past_the_short_grace() -> tuple[FakeUnitOfWork, Server, dt.datetime]:
    # A (stopped, running, assigned) divergence aged past the refused-stop grace but
    # well within the full grace: the window where the recorded verdict is the only
    # thing that decides whether the reconciler acts.
    aged = _NOW - dt.timedelta(seconds=_REFUSED_GRACE + 1)
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    return uow, server, aged


async def test_re_refused_replay_keeps_the_next_tick_on_the_short_grace() -> None:
    # The replay's OWN refusal is recorded too (issue #2478), and that is behaviour,
    # not bookkeeping: the forget() before the replay clears the operator dispatch's
    # verdict, so without re-recording, a stop the Worker refuses AGAIN falls back to
    # waiting the full grace — the exact regression this issue exists to prevent.
    # With it the per-server backoff governs the retry cadence instead.
    uow, server, aged = _stale_stop_past_the_short_grace()
    cp = FakeControlPlane(
        outcomes={"stop": CommandOutcome(status=CommandStatus.INTERNAL, message="boom")}
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock, stop_refusals=_refused(server, aged))

    await reconciler.tick()
    assert [k for k, _, _ in cp.dispatched] == ["stop"]  # refused: no final snapshot

    # Past the backoff window (base 30 s) but still well within the full grace, so
    # only a surviving verdict can let this tick act.
    clock.set(_NOW + dt.timedelta(seconds=31))
    assert (clock.now() - aged) < dt.timedelta(seconds=_GRACE)
    await reconciler.tick()
    assert [k for k, _, _ in cp.dispatched] == ["stop", "stop"]


async def test_timed_out_replay_drops_the_earlier_verdict() -> None:
    # forget() runs before the replay dispatch, not only before the operator's
    # (issue #2478). A replay that TIMES OUT is not the Worker answering — it may
    # still be executing the stop — so the earlier "settled" verdict must not be
    # inherited, or the next tick would re-send on the short grace against a command
    # that really may be in flight (the #930 floor's own failure mode). The next tick
    # is instead back on the full grace and waits.
    uow, server, aged = _stale_stop_past_the_short_grace()
    cp = FakeControlPlane(unavailable_kinds={"stop"})
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock, stop_refusals=_refused(server, aged))

    await reconciler.tick()
    assert [k for k, _, _ in cp.dispatched] == ["stop"]  # attempted, no answer

    # Past the backoff window and still within the full grace: the grace is the only
    # thing that can hold the retry back now, and it must.
    clock.set(_NOW + dt.timedelta(seconds=31))
    assert (clock.now() - aged) < dt.timedelta(seconds=_GRACE)
    await reconciler.tick()
    assert [k for k, _, _ in cp.dispatched] == ["stop"]


# --- an EXPIRED stop keeps the short grace past the lane's report (#3682) ---
#
# An in-time EXPIRED is answered when the server's lane frees, which is usually when
# the command ahead of it reports. So a Worker report lands at about the same time as
# the refusal and, for a plain refusal, would supersede it. EXPIRED proves the stop
# was never applied, so a later report that the server still runs says nothing new:
# only a newer intent commit supersedes it. The short grace is still measured from
# that report (``_since``).

# The instants the tests below share, commit then refusal then report, a second
# apart. A tick at ``_NOW`` is past the short grace from the latest of them and well
# within the full grace from all of them.
_REPORT = _NOW - dt.timedelta(seconds=_REFUSED_GRACE + 1)
_REFUSAL = _REPORT - dt.timedelta(seconds=1)
_COMMIT = _REFUSAL - dt.timedelta(seconds=1)


def _expired(server: Server, at: dt.datetime) -> StopDispatchRefusals:
    refusals = StopDispatchRefusals()
    refusals.record_refusal(
        server.id, outcome=CommandOutcome(status=CommandStatus.EXPIRED), at=at
    )
    return refusals


def _reported_after_the_refusal(observed: ObservedState) -> Server:
    return _server(
        desired=DesiredState.STOPPED,
        observed=observed,
        worker=_WORKER,
        observed_at=_REPORT,
        updated_at=_COMMIT,
    )


async def test_expired_stop_replays_on_the_short_grace_past_a_running_report() -> None:
    uow = FakeUnitOfWork()
    server = _reported_after_the_refusal(ObservedState.RUNNING)
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    assert (_NOW - _COMMIT) < dt.timedelta(seconds=_GRACE)
    await _reconciler(uow, cp, clock, stop_refusals=_expired(server, _REFUSAL)).tick()
    assert [k for k, _, _ in cp.dispatched] == ["stop", "snapshot"]


async def test_plain_refusal_is_superseded_by_a_newer_running_report() -> None:
    # The exemption is EXPIRED-only. Any other refusal says the Worker answered but
    # not that the stop was never applied, so a later ``running`` report is news
    # about the row and the full grace returns (#2478). Identical row and timing to
    # the test above; only the refusal's kind differs.
    uow = FakeUnitOfWork()
    server = _reported_after_the_refusal(ObservedState.RUNNING)
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock, stop_refusals=_refused(server, _REFUSAL)).tick()
    assert cp.dispatched == []


async def test_expired_stop_waits_the_short_grace_from_the_newer_report() -> None:
    uow = FakeUnitOfWork()
    server = _reported_after_the_refusal(ObservedState.RUNNING)
    uow.servers.seed(server)
    cp = FakeControlPlane()
    # Past the short grace from the refusal and the commit, but not from the report.
    clock = FakeClock(_REPORT + dt.timedelta(seconds=_REFUSED_GRACE - 1))
    assert (clock.now() - _REFUSAL) >= dt.timedelta(seconds=_REFUSED_GRACE)
    await _reconciler(uow, cp, clock, stop_refusals=_expired(server, _REFUSAL)).tick()
    assert cp.dispatched == []


async def test_expired_refusal_older_than_the_last_commit_waits_the_full_grace() -> (
    None
):
    # A newer intent commit still supersedes an EXPIRED refusal: it answered a stop
    # the row has since moved past.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
        observed_at=_REFUSAL,
        updated_at=_REPORT,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    refusals = _expired(server, _REFUSAL - dt.timedelta(seconds=1))
    await _reconciler(uow, cp, clock, stop_refusals=refusals).tick()
    assert cp.dispatched == []


@pytest.mark.parametrize("observed", [ObservedState.STOPPING, ObservedState.UNKNOWN])
async def test_expired_refusal_is_superseded_by_a_newer_non_running_report(
    observed: ObservedState,
) -> None:
    # The exception covers only a report that the server still runs. A later
    # stopping/unknown report is news about a stop (another one, or the Worker's own
    # converger), so it supersedes the refusal and the full grace returns. The
    # Worker is connected, so the owed action is ``redispatch_stop``.
    uow = FakeUnitOfWork()
    server = _reported_after_the_refusal(observed)
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock, stop_refusals=_expired(server, _REFUSAL)).tick()
    assert cp.dispatched == []


async def test_expired_refusal_does_not_shorten_a_stale_assignment_clear() -> None:
    # A ``stopped`` report owes ``clear_stale_assignment``, which carries the #847
    # stale-snapshot floor that no stop refusal speaks to: full grace. The refusal
    # ties the report, so it would count as the latest thing known about the row:
    # only the action gate in ``_grace_for`` keeps the short grace away.
    uow = FakeUnitOfWork()
    server = _reported_after_the_refusal(ObservedState.STOPPED)
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock, stop_refusals=_expired(server, _REPORT)).tick()
    assert cp.dispatched == []
    assert uow.servers.by_id[server.id].assigned_worker_id == _WORKER


async def test_operator_stop_answered_expired_converges_on_the_short_grace() -> None:
    # End to end over the SHARED journal: the operator's stop is answered EXPIRED,
    # the lane's release brings a ``running`` report right after it, and the
    # reconciler still replays the stop on the short grace from that report instead
    # of leaving the server running for the full grace.
    uow = FakeUnitOfWork()
    long_running = _NOW - dt.timedelta(seconds=3600)
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
        observed_at=long_running,
        updated_at=long_running,
    )
    uow.servers.seed(server)
    refusals = StopDispatchRefusals()
    clock = FakeClock(_NOW)
    expiring = FakeControlPlane(
        outcomes={
            "stop": CommandOutcome(
                status=worker_status("StopServer", "command_expired"),
                message="expired",
            )
        }
    )
    with pytest.raises(CommandDispatchError):
        await StopServer(
            uow=uow, control_plane=expiring, clock=clock, stop_refusals=refusals
        )(community_id=server.community_id, server_id=server.id)

    report = _NOW + dt.timedelta(seconds=1)
    assert await uow.servers.record_observed_state(
        server.id, ObservedState.RUNNING, report
    )

    cp = FakeControlPlane()
    reconciler = _reconciler(uow, cp, clock, stop_refusals=refusals)
    clock.set(report + dt.timedelta(seconds=_REFUSED_GRACE - 1))
    await reconciler.tick()
    assert cp.dispatched == []

    clock.set(report + dt.timedelta(seconds=_REFUSED_GRACE + 1))
    await reconciler.tick()
    assert [k for k, _, _ in cp.dispatched] == ["stop", "snapshot"]
    assert uow.servers.by_id[server.id].assigned_worker_id is None


# --- short grace for a finished stop whose write failed (issue #3441) -------


class _OutageServerRepository(FakeServerRepository):
    """Raise on the named repository calls, as a database outage would.

    The fake applies a write before its commit and has no rollback, so failing the
    commit would leave the write in place. Failing the call itself leaves the row as
    a failed transaction leaves it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.down: set[str] = set()

    def _fail_if_down(self, name: str) -> None:
        if name in self.down:
            raise ConnectionRefusedError("database is unreachable")

    async def get_by_id(self, server_id: ServerId) -> Server | None:
        self._fail_if_down("get_by_id")
        return await super().get_by_id(server_id)

    async def record_observed_state(
        self,
        server_id: ServerId,
        observed_state: ObservedState,
        observed_at: dt.datetime,
        *,
        unassign: bool = False,
        expected_worker: WorkerId | None = None,
        detail: str = "",
        reason: CrashReason | None = None,
    ) -> bool:
        self._fail_if_down("record_observed_state")
        return await super().record_observed_state(
            server_id,
            observed_state,
            observed_at,
            unassign=unassign,
            expected_worker=expected_worker,
            detail=detail,
            reason=reason,
        )

    async def clear_assignment_after_final_snapshot(
        self, server_id: ServerId, worker_id: WorkerId
    ) -> bool:
        self._fail_if_down("clear_assignment_after_final_snapshot")
        return await super().clear_assignment_after_final_snapshot(server_id, worker_id)

    async def clear_stale_assignment(
        self,
        server_id: ServerId,
        worker_id: WorkerId,
        *,
        observed_at: dt.datetime | None,
    ) -> bool:
        self._fail_if_down("clear_stale_assignment")
        return await super().clear_stale_assignment(
            server_id, worker_id, observed_at=observed_at
        )


@pytest.mark.parametrize(
    ("stop_status", "failing_write", "observed_after", "owed_dispatches"),
    [
        # The observed=stopped write fails: the row still reads running, so the owed
        # action is redispatch_stop, which the Worker answers and the release follows.
        (
            CommandStatus.OK,
            "record_observed_state",
            ObservedState.RUNNING,
            ["stop", "snapshot"],
        ),
        # The release after the final snapshot fails: the row reads (stopped,
        # stopped, assigned), so the owed action is clear_stale_assignment.
        (
            CommandStatus.OK,
            "clear_assignment_after_final_snapshot",
            ObservedState.STOPPED,
            ["snapshot"],
        ),
        # The Worker answers that no instance remains (issue #2448), and the same
        # observed=stopped write fails on that release path.
        (
            CommandStatus.SERVER_NOT_FOUND,
            "record_observed_state",
            ObservedState.RUNNING,
            ["stop", "snapshot"],
        ),
    ],
)
async def test_confirmed_stop_whose_write_failed_converges_on_the_short_grace(
    stop_status: CommandStatus,
    failing_write: str,
    observed_after: ObservedState,
    owed_dispatches: list[str],
) -> None:
    # The Worker confirmed the stop, but the API's write after it failed (a database
    # outage, issue #3441). Nothing is in flight: the stop's round trip ended (#930)
    # and no final snapshot can still be uploading (#847). So the reconciler runs the
    # owed action on the short grace instead of the full one.
    repo = _OutageServerRepository()
    uow = FakeUnitOfWork(servers=repo)
    long_running = _NOW - dt.timedelta(seconds=3600)
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
        observed_at=long_running,
        updated_at=long_running,
    )
    repo.seed(server)
    unrecorded = UnrecordedStops()
    clock = FakeClock(_NOW)
    repo.down = {failing_write}
    with pytest.raises(ConnectionRefusedError):
        await StopServer(
            uow=uow,
            control_plane=FakeControlPlane(
                outcomes={"stop": CommandOutcome(status=stop_status)}
            ),
            clock=clock,
            unrecorded_stops=unrecorded,
        )(community_id=server.community_id, server_id=server.id)
    repo.down = set()
    stored = repo.by_id[server.id]
    assert stored.desired_state is DesiredState.STOPPED
    assert stored.observed_state is observed_after
    assert stored.assigned_worker_id == _WORKER

    cp = FakeControlPlane()
    clock.set(_NOW + dt.timedelta(seconds=_REFUSED_GRACE + 1))
    await _reconciler(uow, cp, clock, unrecorded_stops=unrecorded).tick()

    assert [k for k, _, _ in cp.dispatched] == owed_dispatches
    assert repo.by_id[server.id].assigned_worker_id is None


async def test_failed_late_snapshot_release_converges_on_the_short_grace() -> None:
    # The late result of an abandoned final snapshot means its upload settled, but
    # the release it carries failed to be written (issue #3441). The control plane
    # has already dropped the snapshot's record, so no later result can retry it.
    # The reconciler's stale-stop arm runs on the short grace instead.
    repo = _OutageServerRepository()
    uow = FakeUnitOfWork(servers=repo)
    held_since = _NOW - dt.timedelta(seconds=1)
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.STOPPED,
        worker=_WORKER,
        observed_at=held_since,
        updated_at=held_since,
    )
    repo.seed(server)
    unrecorded = UnrecordedStops()
    clock = FakeClock(_NOW)
    repo.down = {"get_by_id"}
    with pytest.raises(ConnectionRefusedError):
        await StopServer(
            uow=uow,
            control_plane=FakeControlPlane(),
            clock=clock,
            unrecorded_stops=unrecorded,
        ).clear_assignment_after_late_snapshot(
            server_id=server.id, worker_id=_WORKER, succeeded=True, message=None
        )
    repo.down = set()
    assert repo.by_id[server.id].assigned_worker_id == _WORKER

    cp = FakeControlPlane()
    clock.set(_NOW + dt.timedelta(seconds=_REFUSED_GRACE + 1))
    assert (clock.now() - held_since) < dt.timedelta(seconds=_GRACE)
    await _reconciler(uow, cp, clock, unrecorded_stops=unrecorded).tick()

    assert [k for k, _, _ in cp.dispatched] == ["snapshot"]
    assert repo.by_id[server.id].assigned_worker_id is None


def _unrecorded(server: Server, at: dt.datetime) -> UnrecordedStops:
    unrecorded = UnrecordedStops()
    unrecorded.record(server.id, at=at)
    return unrecorded


async def test_recovery_whose_release_failed_retries_on_the_short_grace() -> None:
    # A short-grace recovery dispatches the final snapshot, which forgets the record,
    # and the snapshot settles. The release after it then fails (issue #3619).
    # Nothing is in flight, so the recovery records the finished stop again, and the
    # next tick past the backoff retries on the short grace, not the full one.
    repo = _OutageServerRepository()
    uow = FakeUnitOfWork(servers=repo)
    aged = _NOW - dt.timedelta(seconds=_REFUSED_GRACE + 1)
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.STOPPED,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    repo.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock, unrecorded_stops=_unrecorded(server, aged))

    repo.down = {"clear_stale_assignment"}
    await reconciler.tick()
    repo.down = set()
    assert [k for k, _, _ in cp.dispatched] == ["snapshot"]
    assert repo.by_id[server.id].assigned_worker_id == _WORKER

    # Past the backoff window (base 30 s) and still within the full grace.
    clock.set(_NOW + dt.timedelta(seconds=31))
    assert (clock.now() - aged) < dt.timedelta(seconds=_GRACE)
    await reconciler.tick()
    assert [k for k, _, _ in cp.dispatched] == ["snapshot", "snapshot"]
    assert repo.by_id[server.id].assigned_worker_id is None


@pytest.mark.parametrize(
    ("observed", "failing_write", "recorded"),
    [
        # The process is gone and the Worker's session ended, so no upload survives
        # it and no snapshot was dispatched: nothing is in flight.
        (ObservedState.STOPPED, "clear_stale_assignment", True),
        (ObservedState.CRASHED, "clear_stale_assignment", True),
        # The stop was interrupted, not finished, and the Worker may still be
        # driving it, so these legs keep the full grace.
        (ObservedState.UNKNOWN, "clear_stale_assignment", False),
        (ObservedState.STOPPING, "record_observed_state", False),
    ],
)
async def test_disconnected_recovery_records_a_failed_release_only_when_finished(
    observed: ObservedState, failing_write: str, recorded: bool
) -> None:
    # The recovery's release against a disconnected Worker fails (issue #3619).
    repo = _OutageServerRepository()
    uow = FakeUnitOfWork(servers=repo)
    server = _server(desired=DesiredState.STOPPED, observed=observed, worker=_WORKER)
    repo.seed(server)
    unrecorded = UnrecordedStops()
    cp = FakeControlPlane(connected={_WORKER: False})
    repo.down = {failing_write}
    with pytest.raises(ConnectionRefusedError):
        await StopServer(
            uow=uow,
            control_plane=cp,
            clock=FakeClock(_NOW),
            unrecorded_stops=unrecorded,
        ).clear_stale_assignment(
            community_id=server.community_id,
            server_id=server.id,
            worker_connected=False,
        )
    assert cp.dispatched == []
    assert (unrecorded.recorded_at(server.id) == _NOW) is recorded


async def test_unrecorded_stop_older_than_the_last_report_waits_the_full_grace() -> (
    None
):
    # As with a refusal, the record counts only while it is the most recent thing
    # known about the row. A Worker report or intent commit after it supersedes it.
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_REFUSED_GRACE + 1)
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.STOPPED,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    stale = aged - dt.timedelta(seconds=1)
    await _reconciler(
        uow, cp, FakeClock(_NOW), unrecorded_stops=_unrecorded(server, stale)
    ).tick()
    assert cp.dispatched == []
    assert uow.servers.by_id[server.id].assigned_worker_id == _WORKER


async def test_unrecorded_stop_does_not_shorten_a_start_redispatch() -> None:
    # The record is about a stop. A running-intent divergence keeps the full grace.
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_REFUSED_GRACE + 1)
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    await _reconciler(
        uow, cp, FakeClock(_NOW), unrecorded_stops=_unrecorded(server, aged)
    ).tick()
    assert cp.dispatched == []


class _SnapshotTimesOut(FakeControlPlane):
    """A final snapshot whose dispatch times out while the upload may still run."""

    async def snapshot(
        self,
        *,
        worker_id: WorkerId,
        community_id: CommunityId,
        server_id: ServerId,
        final: bool = False,
    ) -> CommandOutcome:
        self.dispatched.append(("snapshot", worker_id, server_id))
        raise WorkerUnavailableError(str(worker_id.value), upload_may_be_live=True)


async def test_timed_out_final_snapshot_drops_the_unrecorded_stop() -> None:
    # The record is forgotten before every final-snapshot dispatch. One that times
    # out may still be uploading, so releasing on the short grace would break the
    # #847 floor. The next tick waits the full grace instead.
    uow = FakeUnitOfWork()
    aged = _NOW - dt.timedelta(seconds=_REFUSED_GRACE + 1)
    server = _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.STOPPED,
        worker=_WORKER,
        observed_at=aged,
        updated_at=aged,
    )
    uow.servers.seed(server)
    cp = _SnapshotTimesOut()
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock, unrecorded_stops=_unrecorded(server, aged))

    await reconciler.tick()
    assert [k for k, _, _ in cp.dispatched] == ["snapshot"]

    # Past the backoff window (base 30 s) and still within the full grace.
    clock.set(_NOW + dt.timedelta(seconds=31))
    assert (clock.now() - aged) < dt.timedelta(seconds=_GRACE)
    await reconciler.tick()
    assert [k for k, _, _ in cp.dispatched] == ["snapshot"]
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


async def test_busy_start_refusal_does_not_feed_backoff() -> None:
    # BUSY is the Worker saying the id is not free YET — a reservation held by a
    # command from before a reconnect, or a failed-stop orphan it is converging
    # (a start on the in-flight command's own session queues and times out
    # instead, and that does back off) — and that this same start will be
    # accepted once it settles (issue #3209). Nothing failed, so it must not arm a
    # backoff that outlives the busy window and delays the recovery afterwards: the
    # next tick retries.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcomes={"start": CommandOutcome(status=CommandStatus.BUSY)},
        held={(_WORKER, server.id): 2},
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock, store_generation=2)
    await reconciler.tick()  # refused BUSY
    assert [k for k, _, _ in cp.dispatched] == ["start"]
    clock.set(_NOW + dt.timedelta(seconds=1))
    await reconciler.tick()  # well inside what the 30s base backoff would have been
    assert [k for k, _, _ in cp.dispatched] == ["start", "start"]


async def test_expired_start_does_not_feed_backoff() -> None:
    # Issue #3305: an EXPIRED start waited too long behind the server's lane (a
    # restart's stop leg) and was not applied. Like BUSY it is no failure of the
    # start itself, and the next redispatch is accepted once the lane frees, so it
    # must not arm a backoff that keeps the server down after that.
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
                status=worker_status("StartServer", "command_expired")
            )
        },
        held={(_WORKER, server.id): 2},
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock, store_generation=2)
    await reconciler.tick()  # refused EXPIRED
    assert [k for k, _, _ in cp.dispatched] == ["start"]
    clock.set(_NOW + dt.timedelta(seconds=1))
    await reconciler.tick()  # well inside what the 30s base backoff would have been
    assert [k for k, _, _ in cp.dispatched] == ["start", "start"]


async def test_expired_hydrate_does_not_feed_backoff() -> None:
    # The hydrate leg of a reconciler start can expire behind the lane the same way.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcomes={
            "hydrate": CommandOutcome(
                status=worker_status("HydrateTrigger", "command_expired")
            )
        },
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock, store_generation=2)
    await reconciler.tick()  # hydrate refused EXPIRED
    assert [k for k, _, _ in cp.dispatched] == ["hydrate"]
    clock.set(_NOW + dt.timedelta(seconds=1))
    await reconciler.tick()
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "hydrate"]


async def test_unavailable_start_feeds_backoff() -> None:
    # Issue #3673: the Worker's daemon did not answer the start's create or start
    # in time, or a leftover container of the server could not be removed. No
    # other command holds the id, so nothing promises the next start succeeds:
    # it is a failure like any other and backs off, unlike BUSY.
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
                status=worker_status("StartServer", "backend_unavailable")
            )
        },
        held={(_WORKER, server.id): 2},
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock, store_generation=2)
    await reconciler.tick()  # refused, counted -> 30s backoff
    clock.set(_NOW + dt.timedelta(seconds=1))
    await reconciler.tick()  # inside the backoff -> skipped
    assert [k for k, _, _ in cp.dispatched] == ["start"]
    clock.set(_NOW + dt.timedelta(seconds=31))
    await reconciler.tick()  # past it -> retried
    assert [k for k, _, _ in cp.dispatched] == ["start", "start"]


async def test_a_leftover_the_worker_cannot_end_backs_off_with_growing_waits() -> None:
    # Issue #3678: a container a failed start left behind that the Worker cannot
    # end refuses every hydrate of the server, and each hydrate first spends up to
    # a 70 s graceful stop on it. The refusal is the one the Worker emits for
    # {HydrateTrigger, untracked_container} (the contract table, which the
    # Worker's contract test drives through the real instancemanager). It counts
    # toward the backoff, so the redispatches thin out exponentially instead of
    # hitting the Worker every tick.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcomes={
            "hydrate": CommandOutcome(
                status=worker_status("HydrateTrigger", "untracked_container"),
                message="instancemanager: hydrate refused: a container of the server "
                "the Worker does not track may be running against the working set",
            )
        },
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock, store_generation=2)

    def hydrates() -> int:
        assert all(k == "hydrate" for k, _, _ in cp.dispatched)
        return len(cp.dispatched)

    # One tick per second for 200 s: the waits grow 30 s, 60 s, 120 s.
    dispatched_at: list[int] = []
    for second in range(200):
        clock.set(_NOW + dt.timedelta(seconds=second))
        before = hydrates()
        await reconciler.tick()
        if hydrates() > before:
            dispatched_at.append(second)
    assert dispatched_at == [0, 30, 90]


async def test_busy_hydrate_refusal_does_not_feed_backoff() -> None:
    # The control for the test above: a hydrate refused because another command
    # holds the id is contention, which clears on its own, so the next tick
    # retries it (issue #3209).
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcomes={
            "hydrate": CommandOutcome(
                status=worker_status("HydrateTrigger", "command_in_flight")
            )
        },
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock, store_generation=2)
    await reconciler.tick()  # refused BUSY
    clock.set(_NOW + dt.timedelta(seconds=1))
    await reconciler.tick()
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "hydrate"]


async def test_busy_start_refusal_keeps_an_existing_backoff() -> None:
    # Not counting a BUSY refusal is not the same as clearing: a crash-looping
    # server's damping (#343) must survive a busy window.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(held={(_WORKER, server.id): 2})
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock, store_generation=2)
    await reconciler.tick()  # crash counted -> 30s backoff
    cp._outcomes["start"] = CommandOutcome(status=CommandStatus.BUSY)
    clock.set(_NOW + dt.timedelta(seconds=31))
    await reconciler.tick()  # past the backoff -> retried, refused BUSY
    assert len(cp.dispatched) == 2
    del cp._outcomes["start"]
    clock.set(_NOW + dt.timedelta(seconds=62))
    await reconciler.tick()  # crash counted as the SECOND failure -> 60s backoff
    assert len(cp.dispatched) == 3
    clock.set(_NOW + dt.timedelta(seconds=62 + 31))
    await reconciler.tick()  # inside the 60s window -> skipped
    assert len(cp.dispatched) == 3


class _StopLandsDuringHydrate(FakeControlPlane):
    """A Worker whose hydrate answers only after a stop intent committed."""

    def __init__(self, *, uow: FakeUnitOfWork, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._uow = uow

    async def hydrate(
        self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
    ) -> CommandOutcome:
        self._uow.servers.by_id[server_id].desired_state = DesiredState.STOPPED
        return await super().hydrate(
            worker_id=worker_id, community_id=community_id, server_id=server_id
        )


async def test_start_overtaken_by_a_stop_intent_is_skipped_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A stop committed while the redispatched start hydrated (issue #3737): the
    # start answers a transition conflict without sending the start. Nothing
    # failed — the stop intent is newer and owns the row — so the tick neither
    # counts a failure nor warns.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = _StopLandsDuringHydrate(uow=uow)
    reconciler = _reconciler(uow, cp, FakeClock(_NOW))

    with caplog.at_level(logging.WARNING):
        await reconciler.tick()

    assert [k for k, _, _ in cp.dispatched] == ["hydrate"]
    assert server.id not in reconciler._attempts
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_placed_start_overtaken_by_a_stop_intent_is_skipped_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The same race on the orphan placement path (issue #3737): the row is placed,
    # and a stop commits while the placed start hydrates.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker=None,
    )
    uow.servers.seed(server)
    cp = _StopLandsDuringHydrate(uow=uow, place_to=_WORKER)
    reconciler = _reconciler(uow, cp, FakeClock(_NOW))

    with caplog.at_level(logging.WARNING):
        await reconciler.tick()

    assert [k for k, _, _ in cp.dispatched] == ["hydrate"]
    assert server.id not in reconciler._attempts
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


class _IntentFlipsAfterListing(FakeServerRepository):
    """Sets each listed row's intent to ``to`` once the tick has listed it.

    As a stop (``to`` stopped) or a start (``to`` running) committing between the
    tick's listing and its action does. The tick still holds the candidate as
    listed.
    """

    def __init__(self, to: DesiredState) -> None:
        super().__init__()
        self._to = to

    async def list_reconcilable(self) -> list[Server]:
        listed = await super().list_reconcilable()
        for server in listed:
            self.by_id[server.id].desired_state = self._to
        return listed


@pytest.mark.parametrize(
    "worker", [None, _WORKER], ids=["place_and_start", "redispatch_start"]
)
async def test_start_of_an_intent_stopped_after_listing_is_skipped_quietly(
    caplog: pytest.LogCaptureFixture, worker: WorkerId | None
) -> None:
    # An operator stop withdraws the start intent after the tick listed the row
    # and before the start action reads it: an unplaced intent (issue #3815), or
    # one whose start this tick would re-send. The action's entry guard finds no
    # running intent and sends nothing. Nothing failed: the stop owns the row, so
    # the tick neither counts a failure nor warns.
    uow = FakeUnitOfWork(servers=_IntentFlipsAfterListing(DesiredState.STOPPED))
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.STOPPED,
        worker=worker,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(place_to=_WORKER)
    reconciler = _reconciler(uow, cp, FakeClock(_NOW))

    with caplog.at_level(logging.WARNING):
        await reconciler.tick()

    assert cp.dispatched == []
    assert cp.reserved == []
    assert server.id not in reconciler._attempts
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize(
    ("observed", "action"),
    [
        (ObservedState.RUNNING, "redispatch_stop"),
        (ObservedState.STOPPED, "clear_stale_assignment"),
    ],
    ids=["redispatch_stop", "clear_stale_assignment"],
)
async def test_stop_side_action_refused_by_its_entry_guard_still_backs_off(
    caplog: pytest.LogCaptureFixture, observed: ObservedState, action: str
) -> None:
    # The quiet skip above is kept to the two start actions (issue #3815). A
    # stop-side action whose entry guard refuses the row, here because a start
    # committed after the tick listed it, is still counted and logged like any
    # other failed action.
    uow = FakeUnitOfWork(servers=_IntentFlipsAfterListing(DesiredState.RUNNING))
    server = _server(desired=DesiredState.STOPPED, observed=observed, worker=_WORKER)
    uow.servers.seed(server)
    cp = FakeControlPlane()
    reconciler = _reconciler(uow, cp, FakeClock(_NOW))

    with caplog.at_level(logging.WARNING):
        await reconciler.tick()

    assert cp.dispatched == []
    assert reconciler._attempts[server.id].failures == 1
    assert [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and f"reconcile action {action} failed" in r.getMessage()
    ]


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


async def test_first_crash_restart_logs_info_and_repeat_logs_crash_looping_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Issue #3402: a server that crashed ONCE is restarted by the reconciler; that
    # first restart is routine recovery, not a loop, so it must log at INFO worded
    # as a restart after a crash. Only a restart that follows an earlier counted
    # failure — the backoff state already shows repetition — is "crash-looping" at
    # WARNING. The backoff itself is armed both times (#343), unchanged.
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

    def crash_records() -> list[logging.LogRecord]:
        return [
            r
            for r in caplog.records
            if r.name == "mc_server_dashboard_api.servers.application.reconciler"
            and "backing off" in r.getMessage()
        ]

    with caplog.at_level(logging.INFO):
        await reconciler.tick()  # first restart after one crash -> 30s backoff
    assert reconciler._attempts[server.id].failures == 1
    [first] = crash_records()
    assert first.levelno == logging.INFO
    assert first.getMessage() == (
        f"reconcile action redispatch_start restarted server {server.id.value} "
        "after a crash; backing off"
    )
    assert "crash-looping" not in first.getMessage()

    caplog.clear()
    clock.set(_NOW + dt.timedelta(seconds=40))
    with caplog.at_level(logging.INFO):
        await reconciler.tick()  # crashed again past the backoff -> a loop
    assert reconciler._attempts[server.id].failures == 2
    [repeat] = crash_records()
    assert repeat.levelno == logging.WARNING
    assert repeat.getMessage() == (
        "reconcile action redispatch_start dispatched for crash-looping server "
        f"{server.id.value}; backing off"
    )


# --- the crash's reason (issue #3281) -----------------------------------------

# The start intent, then the crash the Worker reported for it.
_INTENT_AT = _PAST_GRACE - dt.timedelta(minutes=5)


@pytest.mark.parametrize(
    "reason",
    [
        CrashReason.FORGE_INSTALL_OUT_OF_MEMORY,
        CrashReason.FORGE_INSTALL_JAVA_INCOMPATIBLE,
    ],
    ids=lambda reason: reason.value,
)
async def test_crash_the_worker_does_not_retry_is_not_restarted(
    reason: CrashReason,
) -> None:
    # The Worker reports these at once instead of retrying the install: the same
    # memory limit or Java runtime fails the next attempt the same way. A restart
    # would only run the failing install again, so the server stays crashed
    # until the operator stops it, fixes the cause and starts it.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
        updated_at=_INTENT_AT,
        crash_reason=reason,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)

    await reconciler.tick()
    # Past any grace and backoff: still not restarted.
    clock.set(_NOW + dt.timedelta(hours=2))
    await reconciler.tick()

    assert cp.dispatched == []
    assert server.id not in reconciler._attempts


@pytest.mark.parametrize(
    "reason",
    [None, CrashReason.FORGE_INSTALL_FAILED],
    ids=["unclassified", "forge_install_failed"],
)
async def test_crash_the_worker_may_retry_is_restarted(
    reason: CrashReason | None,
) -> None:
    # An unclassified crash, and an install failure the Worker could not
    # attribute (it retries a failed installer itself, since a failed download
    # passes), keep the restart and its backoff.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
        updated_at=_INTENT_AT,
        crash_reason=reason,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    reconciler = _reconciler(uow, cp, FakeClock(_NOW))

    await reconciler.tick()

    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]
    assert reconciler._attempts[server.id].failures == 1


async def test_start_intent_newer_than_a_crash_not_retried_is_replayed() -> None:
    # The operator stopped the server after that crash and started it again. The
    # row reads crashed, with the old reason, until the Worker reports the new
    # launch, and a start whose answer was lost keeps the intent for the
    # reconciler to send again. That crash answered an earlier intent, so it
    # holds nothing back.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
        observed_at=_INTENT_AT,
        crash_reason=CrashReason.FORGE_INSTALL_OUT_OF_MEMORY,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane()
    reconciler = _reconciler(uow, cp, FakeClock(_NOW))

    await reconciler.tick()

    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]


@pytest.mark.parametrize(
    ("reason", "restarted"),
    [
        (CrashReason.FORGE_INSTALL_OUT_OF_MEMORY, False),
        (CrashReason.FORGE_INSTALL_JAVA_INCOMPATIBLE, False),
        (CrashReason.FORGE_INSTALL_FAILED, True),
        (None, True),
    ],
    ids=["out-of-memory", "java-incompatible", "install-failed", "unclassified"],
)
async def test_crash_keeps_its_restart_rule_across_a_worker_disconnect(
    reason: CrashReason | None, restarted: bool
) -> None:
    # Issue #3885: the disconnect, or the teardown of a graceful API restart,
    # keeps the crash and its reason, so once the Worker is back the reconciler
    # treats the server as it did before: a crash the Worker does not retry is
    # held, any other is restarted on the same Worker.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.CRASHED,
        worker=_WORKER,
        updated_at=_INTENT_AT,
        crash_reason=reason,
    )
    uow.servers.seed(server)
    await uow.servers.mark_worker_servers_unknown(_WORKER, _NOW)
    stored = await uow.servers.get_by_id(server.id)
    assert stored is not None
    assert (stored.observed_state, stored.crash_reason) == (
        ObservedState.CRASHED,
        reason,
    )
    cp = FakeControlPlane()
    # Past the grace from the disconnect as well as from the crash, so only the
    # crash rule decides.
    reconciler = _reconciler(
        uow, cp, FakeClock(_NOW + dt.timedelta(seconds=_GRACE + 1))
    )

    await reconciler.tick()

    if restarted:
        assert cp.dispatched == [
            ("hydrate", _WORKER, server.id),
            ("start", _WORKER, server.id),
        ]
    else:
        assert cp.dispatched == []


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


async def test_unsettled_absent_backoff_entry_expires() -> None:
    # An entry whose row stays out of the candidate set without ever reading
    # settled (here: stuck in starting) must still be dropped once enough time has
    # lapsed, so the in-memory map does not grow without bound.
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

    uow.servers.by_id[server.id].observed_state = ObservedState.STARTING

    # Just before next_eligible_at + backoff_max slack: entry still retained.
    expiry = entry.next_eligible_at + dt.timedelta(seconds=3600)
    clock.set(expiry - dt.timedelta(seconds=1))
    await reconciler.tick()
    assert server.id in reconciler._attempts

    # At/after the expiry instant: entry is dropped.
    clock.set(expiry)
    await reconciler.tick()
    assert server.id not in reconciler._attempts


async def test_deleted_server_backoff_entry_is_dropped_at_once() -> None:
    # A deleted row can never diverge again, so the next tick that finds it gone
    # drops its entry instead of waiting for the time expiry.
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

    await reconciler.tick()  # crash counted -> entry created
    assert server.id in reconciler._attempts

    del uow.servers.by_id[server.id]
    clock.set(_NOW + dt.timedelta(seconds=10))
    await reconciler.tick()
    assert server.id not in reconciler._attempts


async def test_convergence_just_before_expiry_still_needs_a_full_settled_window() -> (
    None
):
    # Review of #3557: the time expiry must not cut a settled run short. A row
    # that converges just before its entry's expiry instant and crashes again
    # inside the window still escalates instead of starting from the base.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.INTERNAL, message="boom")
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)
    stored = uow.servers.by_id[server.id]

    await reconciler.tick()  # fail 1 -> 30s; expiry instant at +3630s
    assert reconciler._attempts[server.id].failures == 1

    # The Worker drops away; the skipped row keeps its entry.
    cp._connected[_WORKER] = False
    clock.set(_NOW + dt.timedelta(seconds=1000))
    await reconciler.tick()

    # It converges 10s before the expiry instant and is still settled at it.
    stored.observed_state = ObservedState.RUNNING
    cp._connected[_WORKER] = True
    clock.set(_NOW + dt.timedelta(seconds=3620))
    await reconciler.tick()
    clock.set(_NOW + dt.timedelta(seconds=3630))
    await reconciler.tick()
    assert reconciler._attempts[server.id].failures == 1

    # It crashes 11s into the settled run, well inside the 30s window. Ticking
    # every 10s, the crash is acted on once its grace lapses, and the entry must
    # survive the ticks in between.
    crashed_at = _NOW + dt.timedelta(seconds=3631)
    stored.observed_state = ObservedState.CRASHED
    stored.observed_at = crashed_at
    dispatched = len(cp.dispatched)
    at = _NOW + dt.timedelta(seconds=3640)
    while len(cp.dispatched) == dispatched:
        clock.set(at)
        await reconciler.tick()
        assert server.id in reconciler._attempts
        at += dt.timedelta(seconds=10)
    acted = at - dt.timedelta(seconds=10)
    assert acted - crashed_at >= dt.timedelta(seconds=_GRACE)
    entry = reconciler._attempts[server.id]
    assert entry.failures == 2
    assert entry.next_eligible_at == acted + dt.timedelta(seconds=60)


async def test_server_settled_for_its_backoff_starts_from_the_base_again() -> None:
    # Issue #3303: a server that converges with no reconcile action of its own (a
    # restart completing by itself, an operator's fix) must not carry its failure
    # count into a later, unrelated divergence. Once ticks have read it settled for
    # as long as the window its entry last applied, the entry is gone, so the next
    # divergence's first failure arms the base backoff, not a doubled one.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.INTERNAL, message="boom")
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)
    stored = uow.servers.by_id[server.id]

    await reconciler.tick()  # fail 1 -> 30s
    clock.set(_NOW + dt.timedelta(seconds=31))
    await reconciler.tick()  # fail 2 -> 60s
    assert reconciler._attempts[server.id].failures == 2

    # It converges on its own. The tick takes no action; the history is kept
    # until the row has stayed settled for the 60s window the entry last applied.
    stored.observed_state = ObservedState.RUNNING
    dispatched = len(cp.dispatched)
    settled = _NOW + dt.timedelta(seconds=40)
    clock.set(settled)
    await reconciler.tick()
    clock.set(settled + dt.timedelta(seconds=59))
    await reconciler.tick()
    assert reconciler._attempts[server.id].failures == 2
    clock.set(settled + dt.timedelta(seconds=60))
    await reconciler.tick()
    assert len(cp.dispatched) == dispatched
    assert server.id not in reconciler._attempts

    # A later divergence: its failure is the first, at the base backoff.
    later = settled + dt.timedelta(seconds=70)
    stored.observed_state = ObservedState.CRASHED
    stored.observed_at = later - dt.timedelta(seconds=_GRACE + 1)
    clock.set(later)
    await reconciler.tick()
    assert len(cp.dispatched) == dispatched + 1
    assert reconciler._attempts[server.id].failures == 1
    assert reconciler._attempts[server.id].next_eligible_at == later + dt.timedelta(
        seconds=30
    )


async def test_crash_loop_seen_running_mid_cycle_still_escalates() -> None:
    # Issue #3303 must not undo the crash-loop damping (#343) for a server that
    # reaches running and crashes again sooner than its backoff: one tick reading it
    # running is not enough to forgive the history, and the crash that follows
    # breaks the settled run, so every cycle still doubles the window.
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

    at = _NOW
    for failures, delay in ((1, 30), (2, 60), (3, 120)):
        clock.set(at)
        await reconciler.tick()  # restart dispatched, crash counted
        entry = reconciler._attempts[server.id]
        assert entry.failures == failures
        assert entry.next_eligible_at == at + dt.timedelta(seconds=delay)
        # It boots and a tick reads it running, then it crashes before the
        # window it was backed off for has passed.
        stored.observed_state = ObservedState.RUNNING
        clock.set(at + dt.timedelta(seconds=10))
        await reconciler.tick()
        at = entry.next_eligible_at
        stored.observed_state = ObservedState.CRASHED
        stored.observed_at = at - dt.timedelta(seconds=_GRACE + 1)


async def test_settled_run_restarts_when_a_tick_reads_the_row_unsettled() -> None:
    # Issue #3303: the settled time must be one unbroken run. A tick that reads the
    # row unsettled in between (here: back in starting) starts the count over, so
    # two short healthy spells do not add up to the window.
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

    await reconciler.tick()  # crash counted -> 30s window
    stored.observed_state = ObservedState.RUNNING
    clock.set(_NOW + dt.timedelta(seconds=10))
    await reconciler.tick()  # settled run starts
    stored.observed_state = ObservedState.STARTING
    clock.set(_NOW + dt.timedelta(seconds=30))
    await reconciler.tick()  # unsettled: run broken
    stored.observed_state = ObservedState.RUNNING
    clock.set(_NOW + dt.timedelta(seconds=35))
    await reconciler.tick()  # a new run starts
    clock.set(_NOW + dt.timedelta(seconds=64))
    await reconciler.tick()  # 29s into it, 54s since the first: kept
    assert reconciler._attempts[server.id].failures == 1
    clock.set(_NOW + dt.timedelta(seconds=65))
    await reconciler.tick()  # 30s into the run: forgiven
    assert server.id not in reconciler._attempts


async def test_released_stopped_server_forgets_its_backoff() -> None:
    # Issue #3303, stop side: a stop that failed and was then converged elsewhere
    # (an operator stop releasing the row) is settled once the row is unassigned and
    # at rest; after the entry's window its history must not survive into the
    # server's next divergence.
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

    await reconciler.tick()  # stop fails -> entry, 30s
    assert reconciler._attempts[server.id].failures == 1

    stored = uow.servers.by_id[server.id]
    stored.observed_state = ObservedState.STOPPED
    stored.assigned_worker_id = None
    clock.set(_NOW + dt.timedelta(seconds=10))
    await reconciler.tick()
    assert server.id in reconciler._attempts
    clock.set(_NOW + dt.timedelta(seconds=40))
    await reconciler.tick()
    assert server.id not in reconciler._attempts


async def test_persistent_start_failure_still_escalates() -> None:
    # Issue #3303 must not reset a server that never converges: a start that keeps
    # failing is re-read diverged every tick and its backoff keeps doubling.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.INTERNAL, message="boom")
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)

    at = _NOW
    for failures, delay in ((1, 30), (2, 60), (3, 120)):
        clock.set(at)
        await reconciler.tick()
        entry = reconciler._attempts[server.id]
        assert entry.failures == failures
        assert entry.next_eligible_at == at + dt.timedelta(seconds=delay)
        at = entry.next_eligible_at


@pytest.mark.parametrize(
    ("desired", "observed", "worker", "connected", "fresh_report"),
    [
        # A launch still in flight: absent from list_reconcilable, not settled.
        (DesiredState.RUNNING, ObservedState.STARTING, _WORKER, True, False),
        # Skipped by the disconnected-Worker guard.
        (DesiredState.RUNNING, ObservedState.UNKNOWN, _WORKER, False, False),
        # A stop still converging (assignment not released), inside its grace.
        (DesiredState.STOPPED, ObservedState.STOPPED, _WORKER, True, True),
        # A stop owed to a Worker that is gone and still running the server.
        (DesiredState.STOPPED, ObservedState.RUNNING, _WORKER, False, False),
        # Released, but not at rest.
        (DesiredState.STOPPED, ObservedState.STOPPING, None, True, False),
    ],
)
async def test_skipped_unsettled_row_keeps_its_backoff(
    desired: DesiredState,
    observed: ObservedState,
    worker: WorkerId | None,
    connected: bool,
    fresh_report: bool,
) -> None:
    # Issue #3303 clears an entry only for a row read converged. A row the tick
    # merely skips, or does not list, has not settled, so its count must survive.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker=_WORKER,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.INTERNAL, message="boom")
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)
    await reconciler.tick()  # start fails -> entry
    entry = reconciler._attempts[server.id]

    later = _NOW + dt.timedelta(seconds=40)  # past the 30s backoff
    stored = uow.servers.by_id[server.id]
    stored.desired_state = desired
    stored.observed_state = observed
    stored.assigned_worker_id = worker
    stored.observed_at = (
        later - dt.timedelta(seconds=1)
        if fresh_report
        else later - dt.timedelta(seconds=_GRACE + 1)
    )
    cp._connected[_WORKER] = connected
    dispatched = len(cp.dispatched)
    clock.set(later)
    await reconciler.tick()
    assert len(cp.dispatched) == dispatched
    assert reconciler._attempts[server.id] == entry


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


@pytest.mark.parametrize("observed", [ObservedState.UNKNOWN, ObservedState.CRASHED])
async def test_a_placement_whose_hydrate_finds_the_server_live_is_not_re_placed(
    observed: ObservedState,
) -> None:
    # Issue #3865. An unplaced running intent is placed on worker A, which refuses
    # the hydrate because it already runs an instance of the server. Placement does
    # not know that, so releasing A would let the next placement put the server on
    # worker B and start a second instance of the same world there. The tick keeps
    # A, converges observed=running, and counts no failure: a row read crashed
    # before it is not a crash loop. The next tick past grace and any backoff then
    # has nothing to do.
    worker_a, worker_b = WorkerId(uuid.uuid4()), WorkerId(uuid.uuid4())
    uow = FakeUnitOfWork()
    server = _server(desired=DesiredState.RUNNING, observed=observed, worker=None)
    uow.servers.seed(server)
    cp = PlacementSequenceControlPlane(
        places=[worker_a, worker_b],
        hydrate_outcomes={
            worker_a: CommandOutcome(
                status=worker_status("HydrateTrigger", "instance_running"),
                message="instancemanager: server already running",
            )
        },
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)

    await reconciler.tick()
    backed_off = server.id in reconciler._attempts
    clock.set(_NOW + dt.timedelta(seconds=_GRACE + 1))
    await reconciler.tick()

    assert cp.dispatched == [("hydrate", worker_a, server.id)]
    stored = uow.servers.by_id[server.id]
    assert stored.assigned_worker_id == worker_a
    assert stored.observed_state is ObservedState.RUNNING
    assert not backed_off


async def test_a_failed_stop_of_a_server_kept_on_its_worker_is_sent_again() -> None:
    # Issue #3865. A user start found the server already live on its Worker and
    # kept it there. The operator's stop then fails to dispatch, leaving the stop
    # intent on that Worker. The reconciler sends the stop again. It must not take
    # the final snapshot and release the row, as it would for the row's earlier
    # observed=stopped: no start was sent, so no record of one stands in the way,
    # and the release would leave the instance running under no Worker.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED, observed=ObservedState.STOPPED, worker=None
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(
        place_to=_WORKER,
        outcomes={
            "hydrate": CommandOutcome(
                status=worker_status("HydrateTrigger", "instance_running"),
                message="instancemanager: server already running",
            ),
            "stop": CommandOutcome(status=CommandStatus.INTERNAL, message="boom"),
        },
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)
    with pytest.raises(InvalidLifecycleTransitionError):
        await reconciler.make_start_server()(
            community_id=server.community_id, server_id=server.id
        )
    with pytest.raises(CommandDispatchError):
        await reconciler.make_stop_server()(
            community_id=server.community_id, server_id=server.id
        )
    cp.dispatched.clear()
    clock.set(_NOW + dt.timedelta(seconds=_GRACE + 1))

    await reconciler.tick()

    assert cp.dispatched == [("stop", _WORKER, server.id)]
    assert uow.servers.by_id[server.id].assigned_worker_id == _WORKER


# The hydrate refusals that leave an instance of the server on the placed Worker
# possible (issue #3890), as the contract table names their Worker states.
_HYDRATE_REFUSALS_KEEPING_THE_WORKER = [
    pytest.param("command_in_flight", id="busy-in-flight"),
    pytest.param("orphan_pending", id="busy-orphan"),
    pytest.param("untracked_container", id="unavailable"),
    pytest.param("command_expired", id="expired"),
]


def _hydrate_refused(precondition: str) -> CommandOutcome:
    return CommandOutcome(
        status=worker_status("HydrateTrigger", precondition), message="refused"
    )


@pytest.mark.parametrize("entry", ["placement", "user-start"])
@pytest.mark.parametrize("precondition", _HYDRATE_REFUSALS_KEEPING_THE_WORKER)
async def test_a_placement_whose_hydrate_may_meet_an_instance_retries_that_worker(
    precondition: str, entry: str
) -> None:
    # Issue #3890. The reconciler's placement of an unplaced running intent, or a
    # user start, places the server on worker A, which refuses the hydrate with
    # an answer that leaves an instance of the server there possible. A is kept,
    # so each tick past the grace and any backoff retries A, never worker B,
    # which the next placement would pick. Once A's reservation, orphan,
    # leftover or queue clears, the retry hydrates and starts the server on A.
    worker_a, worker_b = WorkerId(uuid.uuid4()), WorkerId(uuid.uuid4())
    uow = FakeUnitOfWork()
    server = _server(
        desired=(
            DesiredState.RUNNING if entry == "placement" else DesiredState.STOPPED
        ),
        observed=ObservedState.UNKNOWN,
        worker=None,
    )
    uow.servers.seed(server)
    hydrate_outcomes = {worker_a: _hydrate_refused(precondition)}
    cp = PlacementSequenceControlPlane(
        places=[worker_a, worker_b], hydrate_outcomes=hydrate_outcomes
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)

    if entry == "placement":
        await reconciler.tick()  # placed on A and refused
    else:
        with pytest.raises(CommandDispatchError):
            await reconciler.make_start_server()(
                community_id=server.community_id, server_id=server.id
            )
    clock.set(_NOW + dt.timedelta(seconds=_GRACE + 1))
    await reconciler.tick()  # A again, refused again
    del hydrate_outcomes[worker_a]
    clock.set(_NOW + dt.timedelta(seconds=2 * (_GRACE + 1)))
    await reconciler.tick()  # A answers

    assert cp.dispatched == [
        ("hydrate", worker_a, server.id),
        ("hydrate", worker_a, server.id),
        ("hydrate", worker_a, server.id),
        ("start", worker_a, server.id),
    ]


@pytest.mark.parametrize("precondition", _HYDRATE_REFUSALS_KEEPING_THE_WORKER)
async def test_a_kept_placement_whose_worker_disconnects_waits_for_the_release(
    precondition: str,
) -> None:
    # Issue #3890. Worker A kept the server after refusing its hydrate, then its
    # session ended. While A is gone the reconciler places the server nowhere
    # else. What releases it is what releases any server of a Worker that does
    # not come back (CONTROL_PLANE.md Section 4.4): an operator stop, which
    # cannot reach A, and the release the reconciler then owes that stop. Only a
    # start after that places the server on worker B.
    worker_a, worker_b = WorkerId(uuid.uuid4()), WorkerId(uuid.uuid4())
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING, observed=ObservedState.UNKNOWN, worker=None
    )
    uow.servers.seed(server)
    connected = {worker_a: True, worker_b: True}
    cp = PlacementSequenceControlPlane(
        places=[worker_a, worker_b],
        hydrate_outcomes={worker_a: _hydrate_refused(precondition)},
        connected=connected,
        # The stop cannot reach a Worker with no session (the fake still records
        # the attempt).
        unavailable_kinds={"stop"},
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)
    await reconciler.tick()  # placed on A and refused
    connected[worker_a] = False
    clock.set(_NOW + dt.timedelta(seconds=_GRACE + 1))
    await reconciler.tick()  # A is gone: nothing to do

    with pytest.raises(WorkerUnavailableError):
        await reconciler.make_stop_server()(
            community_id=server.community_id, server_id=server.id
        )
    clock.set(_NOW + dt.timedelta(seconds=2 * (_GRACE + 1)))
    await reconciler.tick()  # the stop's release
    await reconciler.make_start_server()(
        community_id=server.community_id, server_id=server.id
    )

    assert cp.dispatched == [
        ("hydrate", worker_a, server.id),
        ("stop", worker_a, server.id),
        ("hydrate", worker_b, server.id),
        ("start", worker_b, server.id),
    ]


@pytest.mark.parametrize("entry", ["placement", "user-start"])
async def test_a_kept_server_that_read_running_without_a_worker_is_retried(
    entry: str,
) -> None:
    # Issue #3890. A row with no Worker can still read observed=running, which no
    # Worker reports on any more (#3740). Kept on worker A after a refused
    # hydrate, it would then read converged, and nothing would retry the start
    # that was never sent. The keep records observed=unknown, so the reconciler
    # retries the start on A once the grace lapses.
    worker_a, worker_b = WorkerId(uuid.uuid4()), WorkerId(uuid.uuid4())
    uow = FakeUnitOfWork()
    server = _server(
        desired=(
            DesiredState.RUNNING if entry == "placement" else DesiredState.STOPPED
        ),
        observed=ObservedState.RUNNING,
        worker=None,
    )
    uow.servers.seed(server)
    cp = PlacementSequenceControlPlane(
        places=[worker_a, worker_b],
        hydrate_outcomes={worker_a: _hydrate_refused("command_in_flight")},
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)
    if entry == "placement":
        await reconciler.tick()
    else:
        with pytest.raises(CommandDispatchError):
            await reconciler.make_start_server()(
                community_id=server.community_id, server_id=server.id
            )
    clock.set(_NOW + dt.timedelta(seconds=_GRACE + 1))

    await reconciler.tick()

    assert cp.dispatched == [
        ("hydrate", worker_a, server.id),
        ("hydrate", worker_a, server.id),
    ]


async def test_a_failed_stop_after_a_refused_hydrate_is_sent_again() -> None:
    # Issue #3890. A user start kept the server on its Worker after a hydrate
    # refused over a failed-stop orphan, which may still be running. The
    # operator's stop then fails to dispatch. The reconciler sends the stop again.
    # It must not take the final snapshot and release the row, as it would for
    # the row's earlier observed=stopped: no start was sent, and the release
    # would leave that orphan running under no Worker.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.STOPPED, observed=ObservedState.STOPPED, worker=None
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(
        place_to=_WORKER,
        outcomes={
            "hydrate": _hydrate_refused("orphan_pending"),
            "stop": CommandOutcome(status=CommandStatus.INTERNAL, message="boom"),
        },
    )
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock)
    with pytest.raises(CommandDispatchError):
        await reconciler.make_start_server()(
            community_id=server.community_id, server_id=server.id
        )
    with pytest.raises(CommandDispatchError):
        await reconciler.make_stop_server()(
            community_id=server.community_id, server_id=server.id
        )
    cp.dispatched.clear()
    clock.set(_NOW + dt.timedelta(seconds=_GRACE + 1))

    await reconciler.tick()

    assert cp.dispatched == [("stop", _WORKER, server.id)]
    assert uow.servers.by_id[server.id].assigned_worker_id == _WORKER


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
        stop_refusals=StopDispatchRefusals(),
        unrecorded_stops=UnrecordedStops(),
        grace_seconds=_GRACE,
        held_start_grace_seconds=_HELD_GRACE,
        refused_stop_grace_seconds=_REFUSED_GRACE,
        restart_timeout_seconds=_RESTART_BUDGET,
        backoff_base_seconds=30,
        backoff_max_seconds=3600,
        listener_started_at=_STARTED_LONG_AGO,
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
        stop_refusals=StopDispatchRefusals(),
        unrecorded_stops=UnrecordedStops(),
        grace_seconds=_GRACE,
        held_start_grace_seconds=_HELD_GRACE,
        refused_stop_grace_seconds=_REFUSED_GRACE,
        restart_timeout_seconds=_RESTART_BUDGET,
        backoff_base_seconds=30,
        backoff_max_seconds=3600,
        listener_started_at=_STARTED_LONG_AGO,
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
    # -> converges to stopped and unassigns. Since issue #2448 that release takes the
    # final snapshot first, exactly as the confirmed-stop and crashed-release legs
    # do: the process is gone but the Worker's retained scratch still holds whatever
    # the interrupted stop never captured.
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
    assert [k for k, _, _ in cp.dispatched] == ["stop", "snapshot"]
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


# --- a full grace for each Worker after an API start (issue #3916) ---------
#
# The reconciler's first tick runs as the API starts, before any Worker has
# registered with it, so every Worker reads disconnected then. Each stopped-intent
# row a disconnected Worker's release covers is held until a full grace has passed
# since this process's listener started, so a Worker that re-registers within it
# still gets the final snapshot or the stop.

_RELEASED_WHEN_AWAY = [
    ObservedState.STOPPED,
    ObservedState.CRASHED,
    ObservedState.UNKNOWN,
    ObservedState.STOPPING,
]
# What a connected Worker is sent for each: the final snapshot of a process that is
# gone, or the stop (then its final snapshot) of one that may not be.
_OWED_WHEN_BACK = {
    ObservedState.STOPPED: ["snapshot"],
    ObservedState.CRASHED: ["snapshot"],
    ObservedState.UNKNOWN: ["stop", "snapshot"],
    ObservedState.STOPPING: ["stop", "snapshot"],
}


def _stop_side_row(observed: ObservedState, *, since: dt.datetime) -> Server:
    return _server(
        desired=DesiredState.STOPPED,
        observed=observed,
        worker=_WORKER,
        observed_at=since,
        updated_at=since,
    )


@pytest.mark.parametrize("observed", _RELEASED_WHEN_AWAY, ids=lambda s: s.value)
async def test_the_boot_tick_releases_nothing_for_a_worker_not_back_yet(
    observed: ObservedState,
) -> None:
    # The row's own grace lapsed before this process started.
    uow = FakeUnitOfWork()
    server = _stop_side_row(observed, since=_PAST_GRACE)
    uow.servers.seed(server)
    cp = FakeControlPlane(connected={_WORKER: False})
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock, listener_started_at=_NOW).tick()
    assert cp.dispatched == []
    assert uow.servers.by_id[server.id].assigned_worker_id == _WORKER


@pytest.mark.parametrize("observed", _RELEASED_WHEN_AWAY, ids=lambda s: s.value)
async def test_a_worker_back_within_a_grace_of_the_api_start_keeps_its_row(
    observed: ObservedState,
) -> None:
    uow = FakeUnitOfWork()
    server = _stop_side_row(observed, since=_PAST_GRACE)
    uow.servers.seed(server)
    connected = {_WORKER: False}
    cp = FakeControlPlane(connected=connected)
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock, listener_started_at=_NOW)
    await reconciler.tick()
    clock.set(_NOW + dt.timedelta(seconds=_GRACE - 1))
    await reconciler.tick()
    assert cp.dispatched == []
    assert uow.servers.by_id[server.id].assigned_worker_id == _WORKER

    connected[_WORKER] = True
    await reconciler.tick()
    assert [k for k, _, _ in cp.dispatched] == _OWED_WHEN_BACK[observed]
    assert uow.servers.by_id[server.id].assigned_worker_id is None


@pytest.mark.parametrize("observed", _RELEASED_WHEN_AWAY, ids=lambda s: s.value)
async def test_a_worker_that_never_returns_is_released_a_grace_after_the_api_start(
    observed: ObservedState,
) -> None:
    uow = FakeUnitOfWork()
    server = _stop_side_row(observed, since=_PAST_GRACE)
    uow.servers.seed(server)
    cp = FakeControlPlane(connected={_WORKER: False})
    clock = FakeClock(_NOW)
    reconciler = _reconciler(uow, cp, clock, listener_started_at=_NOW)
    await reconciler.tick()
    clock.set(_NOW + dt.timedelta(seconds=_GRACE - 1))
    await reconciler.tick()
    assert uow.servers.by_id[server.id].assigned_worker_id == _WORKER

    clock.set(_NOW + dt.timedelta(seconds=_GRACE))
    await reconciler.tick()
    assert cp.dispatched == []
    assert uow.servers.by_id[server.id].assigned_worker_id is None


@pytest.mark.parametrize("observed", _RELEASED_WHEN_AWAY, ids=lambda s: s.value)
async def test_a_disconnected_workers_row_still_waits_its_own_grace(
    observed: ObservedState,
) -> None:
    # Long after the API start, the row's own stamp is the later of the two.
    uow = FakeUnitOfWork()
    server = _stop_side_row(observed, since=_NOW - dt.timedelta(seconds=_GRACE - 1))
    uow.servers.seed(server)
    cp = FakeControlPlane(connected={_WORKER: False})
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock).tick()
    assert uow.servers.by_id[server.id].assigned_worker_id == _WORKER


@pytest.mark.parametrize("observed", _RELEASED_WHEN_AWAY, ids=lambda s: s.value)
async def test_a_connected_workers_row_is_not_held_by_the_api_start(
    observed: ObservedState,
) -> None:
    uow = FakeUnitOfWork()
    server = _stop_side_row(observed, since=_PAST_GRACE)
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock, listener_started_at=_NOW).tick()
    assert [k for k, _, _ in cp.dispatched] == _OWED_WHEN_BACK[observed]
    assert uow.servers.by_id[server.id].assigned_worker_id is None


async def test_a_row_with_no_worker_is_placed_at_the_boot_tick() -> None:
    # No Worker is assigned, so there is none to wait for.
    uow = FakeUnitOfWork()
    server = _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.UNKNOWN,
        worker=None,
    )
    uow.servers.seed(server)
    cp = FakeControlPlane(place_to=_WORKER)
    clock = FakeClock(_NOW)
    await _reconciler(uow, cp, clock, listener_started_at=_NOW).tick()
    assert [k for k, _, _ in cp.dispatched] == ["hydrate", "start"]
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
