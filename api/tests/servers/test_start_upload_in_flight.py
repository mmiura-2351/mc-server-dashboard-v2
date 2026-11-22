"""A start's wait for its Worker's uploads and the hydrate budget (issue #3950).

Before a start hydrates a Worker, it waits for that Worker's snapshot uploads of
the server still in flight. The wait and the hydrate share one deadline, the
hydrate budget from the moment the wait begins, measured on the monotonic clock:
the hydrate gets what the wait left. A wait that leaves no more than the command
expiry margin sends nothing to the Worker, and every entry point keeps the
server on it for the reconciler to retry there: nothing is compensated, released
or placed again. A start whose intent was withdrawn during the wait sends nothing
either, and answers the conflict. The data-plane side, with a real upload, is in
``tests/dataplane/test_start_waits_for_worker_uploads.py``.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable

import pytest

from mc_server_dashboard_api.servers.application.lifecycle import StartServer
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    LifecycleTransitionConflictError,
    ServerError,
    SnapshotUploadInFlightError,
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
from tests.servers.contract_table import worker_message, worker_status
from tests.servers.fakes import (
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeStoreGenerationReader,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 11, 21, 12, 0, tzinfo=dt.timezone.utc)
_WORKER = WorkerId(uuid.uuid4())
_OTHER_WORKER = WorkerId(uuid.uuid4())
_BUDGET = 600.0
# The command expiry margin (fleet ``EXPIRY_MARGIN_SECONDS``): a command handed no
# more than this is dropped unsent.
_MARGIN = 1.0


class _Uploads(FakeStoreGenerationReader):
    """A store whose wait for the Worker's uploads takes ``takes`` seconds.

    The wait moves ``clock``'s monotonic reading on by that much, runs
    ``during``, then answers ``settled``.
    """

    def __init__(
        self,
        clock: FakeClock,
        *,
        takes: float,
        settled: bool,
        during: Callable[[], None] = lambda: None,
    ) -> None:
        super().__init__(generation=1)
        self._clock = clock
        self._takes = takes
        self._settled = settled
        self._during = during
        self.waited: list[tuple[WorkerId, float]] = []

    async def uploads_settled(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        worker_id: WorkerId,
        timeout: float,
    ) -> bool:
        self.waited.append((worker_id, timeout))
        self._clock.advance_monotonic(self._takes)
        self._during()
        return self._settled


def _server(*, desired: DesiredState, on_worker: bool) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=desired,
        observed_state=ObservedState.STOPPED,
        observed_at=_NOW - dt.timedelta(minutes=1),
        assigned_worker_id=_WORKER if on_worker else None,
        created_at=_NOW,
        updated_at=_NOW - dt.timedelta(minutes=1),
    )


def _start(
    uow: FakeUnitOfWork, cp: FakeControlPlane, reader: _Uploads, clock: FakeClock
) -> StartServer:
    return StartServer(
        uow=uow,
        control_plane=cp,
        clock=clock,
        jar_provisioner=FakeJarProvisioner(),
        store_generation=reader,
        file_store=FakeFileStore(seed_eula=True),
    )


def _kinds(cp: FakeControlPlane) -> list[str]:
    return [kind for kind, _, _ in cp.dispatched]


async def test_a_wait_that_uses_part_of_the_budget_leaves_the_hydrate_the_rest() -> (
    None
):
    server = _server(desired=DesiredState.STOPPED, on_worker=False)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(place_to=_WORKER, hydrate_timeout_seconds=_BUDGET)
    clock = FakeClock(_NOW)
    reader = _Uploads(clock, takes=400, settled=True)

    await _start(uow, cp, reader, clock)(
        community_id=server.community_id, server_id=server.id
    )

    assert reader.waited == [(_WORKER, _BUDGET)]
    assert _kinds(cp) == ["hydrate", "start"]
    assert cp.hydrate_timeouts == [_BUDGET - 400]


async def test_a_wall_clock_step_during_the_wait_does_not_shorten_the_hydrate() -> None:
    # The remainder is measured on the monotonic clock: the wall clock jumping an
    # hour ahead during a wait that took no time leaves the hydrate the budget.
    server = _server(desired=DesiredState.STOPPED, on_worker=False)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(place_to=_WORKER, hydrate_timeout_seconds=_BUDGET)
    clock = FakeClock(_NOW)
    reader = _Uploads(
        clock,
        takes=0,
        settled=True,
        during=lambda: clock.set(_NOW + dt.timedelta(hours=1)),
    )

    await _start(uow, cp, reader, clock)(
        community_id=server.community_id, server_id=server.id
    )

    assert _kinds(cp) == ["hydrate", "start"]
    assert cp.hydrate_timeouts == [_BUDGET]


async def test_a_wait_that_leaves_just_over_the_margin_still_hydrates() -> None:
    server = _server(desired=DesiredState.STOPPED, on_worker=False)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(place_to=_WORKER, hydrate_timeout_seconds=_BUDGET)
    clock = FakeClock(_NOW)
    reader = _Uploads(clock, takes=_BUDGET - _MARGIN - 0.5, settled=True)

    await _start(uow, cp, reader, clock)(
        community_id=server.community_id, server_id=server.id
    )

    assert _kinds(cp) == ["hydrate", "start"]
    assert cp.hydrate_timeouts == [_MARGIN + 0.5]


@pytest.mark.parametrize(
    ("takes", "settled"),
    [(_BUDGET, False), (_BUDGET, True), (_BUDGET - _MARGIN, True)],
    ids=["ran-out", "landed-at-it", "left-only-the-margin"],
)
async def test_a_wait_that_uses_the_whole_budget_sends_no_hydrate(
    takes: float, settled: bool
) -> None:
    # The wait ran out at the budget, or the uploads left just as it did, or they
    # left no more than the command expiry margin, which a command is never sent
    # with: either way nothing of the budget is left for a hydrate.
    server = _server(desired=DesiredState.STOPPED, on_worker=False)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(place_to=_WORKER, hydrate_timeout_seconds=_BUDGET)
    clock = FakeClock(_NOW)
    reader = _Uploads(clock, takes=takes, settled=settled)

    with pytest.raises(SnapshotUploadInFlightError):
        await _start(uow, cp, reader, clock)(
            community_id=server.community_id, server_id=server.id
        )

    assert reader.waited == [(_WORKER, _BUDGET)]
    assert cp.dispatched == []


async def test_a_start_keeps_its_placement_when_the_wait_runs_out() -> None:
    server = _server(desired=DesiredState.STOPPED, on_worker=False)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(place_to=_WORKER, hydrate_timeout_seconds=_BUDGET)
    clock = FakeClock(_NOW)
    reader = _Uploads(clock, takes=_BUDGET, settled=False)

    with pytest.raises(SnapshotUploadInFlightError):
        await _start(uow, cp, reader, clock)(
            community_id=server.community_id, server_id=server.id
        )

    assert cp.dispatched == []
    assert (cp.incremented, cp.released, cp.decremented) == ([_WORKER], [], [])
    row = uow.servers.by_id[server.id]
    assert (row.desired_state, row.assigned_worker_id, row.observed_state) == (
        DesiredState.RUNNING,
        _WORKER,
        ObservedState.UNKNOWN,
    )


async def test_an_orphan_placement_keeps_its_worker_when_the_wait_runs_out() -> None:
    server = _server(desired=DesiredState.RUNNING, on_worker=False)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(place_to=_WORKER, hydrate_timeout_seconds=_BUDGET)
    clock = FakeClock(_NOW)
    reader = _Uploads(clock, takes=_BUDGET, settled=False)

    with pytest.raises(SnapshotUploadInFlightError):
        await _start(uow, cp, reader, clock).place_and_start(
            community_id=server.community_id, server_id=server.id
        )

    assert reader.waited == [(_WORKER, _BUDGET)]
    assert cp.dispatched == []
    assert (cp.incremented, cp.released, cp.decremented) == ([_WORKER], [], [])
    row = uow.servers.by_id[server.id]
    assert (row.desired_state, row.assigned_worker_id, row.observed_state) == (
        DesiredState.RUNNING,
        _WORKER,
        ObservedState.UNKNOWN,
    )


async def test_a_redispatch_changes_nothing_when_the_wait_runs_out() -> None:
    server = _server(desired=DesiredState.RUNNING, on_worker=True)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(hydrate_timeout_seconds=_BUDGET)
    clock = FakeClock(_NOW)
    reader = _Uploads(clock, takes=_BUDGET, settled=False)

    with pytest.raises(SnapshotUploadInFlightError):
        await _start(uow, cp, reader, clock).redispatch_start(
            community_id=server.community_id, server_id=server.id
        )

    assert reader.waited == [(_WORKER, _BUDGET)]
    assert cp.dispatched == []
    assert (cp.incremented, cp.released, cp.decremented) == ([], [], [])
    row = uow.servers.by_id[server.id]
    assert (row.desired_state, row.assigned_worker_id, row.observed_state) == (
        DesiredState.RUNNING,
        _WORKER,
        ObservedState.STOPPED,
    )


async def test_a_replayed_start_keeps_its_worker_as_it_was_when_the_wait_runs_out() -> (
    None
):
    # Issue #2499's replay: the start skipped the hydrate, the Worker refused it
    # for a working set it does not hold, and the replay's wait runs out. A start
    # was sent, so the server is kept as after any start sent: nothing is undone,
    # and no observed state is written over what the Worker may report.
    server = _server(desired=DesiredState.STOPPED, on_worker=False)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(
        place_to=_WORKER,
        held={(_WORKER, server.id): 1},
        outcomes={
            "start": CommandOutcome(
                status=CommandStatus.SERVER_NOT_FOUND,
                message=worker_message("working_set_absent.launch")
                % "/var/lib/mcsd/scratch/s",
            )
        },
        hydrate_timeout_seconds=_BUDGET,
    )
    clock = FakeClock(_NOW)
    reader = _Uploads(clock, takes=_BUDGET, settled=False)

    with pytest.raises(SnapshotUploadInFlightError):
        await _start(uow, cp, reader, clock)(
            community_id=server.community_id, server_id=server.id
        )

    assert _kinds(cp) == ["start"]
    assert reader.waited == [(_WORKER, _BUDGET)]
    assert (cp.incremented, cp.released, cp.decremented) == ([_WORKER], [], [])
    row = uow.servers.by_id[server.id]
    assert (
        row.desired_state,
        row.assigned_worker_id,
        row.observed_state,
        row.awaiting_hydrate,
    ) == (DesiredState.RUNNING, _WORKER, ObservedState.STOPPED, True)


def _withdraw(uow: FakeUnitOfWork, server: Server, how: str) -> Callable[[], None]:
    """What withdraws the start's intent during its wait, as a committed write."""

    def _write() -> None:
        row = uow.servers.by_id[server.id]
        if how == "stopped":
            row.desired_state = DesiredState.STOPPED
            row.assigned_worker_id = None
        else:
            row.assigned_worker_id = _OTHER_WORKER
        row.updated_at = _NOW + dt.timedelta(seconds=1)

    return _write


@pytest.mark.parametrize("settled", [True, False], ids=["landed", "ran-out"])
@pytest.mark.parametrize("how", ["stopped", "moved"])
@pytest.mark.parametrize("entry", ["start", "place_and_start", "redispatch_start"])
async def test_a_start_whose_intent_was_withdrawn_during_the_wait_sends_nothing(
    entry: str, how: str, settled: bool
) -> None:
    # A stop committed and released the row while the start waited, or the row
    # moved to another Worker. Whether the wait then ended or ran out, the start
    # is no longer this one's: it answers the conflict, with no hydrate sent and
    # nothing recorded, reverted or released, since the write that withdrew it
    # owns the row.
    server = _server(
        desired=(DesiredState.STOPPED if entry == "start" else DesiredState.RUNNING),
        on_worker=entry == "redispatch_start",
    )
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(place_to=_WORKER, hydrate_timeout_seconds=_BUDGET)
    clock = FakeClock(_NOW)
    reader = _Uploads(
        clock, takes=10, settled=settled, during=_withdraw(uow, server, how)
    )
    start = _start(uow, cp, reader, clock)
    entries: dict[str, Callable[..., Awaitable[Server]]] = {
        "start": start,
        "place_and_start": start.place_and_start,
        "redispatch_start": start.redispatch_start,
    }
    run = entries[entry]
    placed = [] if entry == "redispatch_start" else [_WORKER]

    with pytest.raises(LifecycleTransitionConflictError):
        await run(community_id=server.community_id, server_id=server.id)

    assert reader.waited == [(_WORKER, _BUDGET)]
    assert cp.dispatched == []
    assert (cp.incremented, cp.released, cp.decremented) == (placed, [], [])
    row = uow.servers.by_id[server.id]
    assert row.observed_state is ObservedState.STOPPED
    assert row.updated_at == _NOW + dt.timedelta(seconds=1)


def _hydrate_answers(precondition: str) -> CommandOutcome:
    return CommandOutcome(
        status=worker_status("HydrateTrigger", precondition), message="refused"
    )


# What a re-sent start leaves of the mark: cleared, set, or as it found it.
_PRIOR = None


class _HydrateRaises(FakeControlPlane):
    """A control plane whose hydrate raises ``error``."""

    def __init__(self, error: WorkerUnavailableError) -> None:
        super().__init__(hydrate_timeout_seconds=_BUDGET)
        self._error = error

    async def hydrate(
        self,
        *,
        worker_id: WorkerId,
        community_id: CommunityId,
        server_id: ServerId,
        timeout_seconds: float | None = None,
    ) -> CommandOutcome:
        raise self._error


def _hydrate_raising(
    *, upload_may_be_live: bool = False, session_ended_in_flight: bool = False
) -> Callable[[], FakeControlPlane]:
    return lambda: _HydrateRaises(
        WorkerUnavailableError(
            "worker",
            upload_may_be_live=upload_may_be_live,
            session_ended_in_flight=session_ended_in_flight,
        )
    )


def _hydrate_answering(
    outcome: CommandOutcome | None,
) -> Callable[[], FakeControlPlane]:
    return lambda: FakeControlPlane(
        hydrate_timeout_seconds=_BUDGET,
        outcomes={} if outcome is None else {"hydrate": outcome},
    )


@pytest.mark.parametrize(
    ("make_cp", "settled", "stopped", "marked"),
    [
        pytest.param(_hydrate_answering(None), True, False, False, id="hydrated"),
        pytest.param(
            _hydrate_answering(_hydrate_answers("command_in_flight")),
            True,
            False,
            _PRIOR,
            id="busy",
        ),
        pytest.param(
            _hydrate_answering(_hydrate_answers("untracked_container")),
            True,
            False,
            _PRIOR,
            id="unavailable",
        ),
        pytest.param(
            _hydrate_answering(_hydrate_answers("command_expired")),
            True,
            False,
            _PRIOR,
            id="expired",
        ),
        pytest.param(
            _hydrate_answering(_hydrate_answers("instance_running")),
            True,
            False,
            False,
            id="instance-running",
        ),
        pytest.param(_hydrate_answering(None), False, False, _PRIOR, id="wait-ran-out"),
        pytest.param(
            _hydrate_answering(None), True, True, _PRIOR, id="stopped-during-the-wait"
        ),
        pytest.param(
            _hydrate_raising(), True, False, _PRIOR, id="worker-not-connected"
        ),
        pytest.param(
            _hydrate_answering(
                CommandOutcome(status=CommandStatus.TRANSFER_FAILED, message="unpack")
            ),
            True,
            False,
            True,
            id="transfer-failed",
        ),
        pytest.param(
            _hydrate_raising(upload_may_be_live=True), True, False, True, id="timed-out"
        ),
        pytest.param(
            _hydrate_raising(session_ended_in_flight=True),
            True,
            False,
            True,
            id="session-ended-in-flight",
        ),
    ],
)
@pytest.mark.parametrize("prior", [False, True], ids=["unmarked", "marked"])
async def test_a_resent_start_keeps_the_mark_only_if_its_hydrate_may_have_swapped(
    prior: bool,
    make_cp: Callable[[], FakeControlPlane],
    settled: bool,
    stopped: bool,
    marked: bool | None,
) -> None:
    # Issue #4024. A re-sent start that hydrates marks the row
    # ``awaiting_hydrate`` before its wait. Then:
    # - a hydrate that landed clears the mark, as does an instance the Worker
    #   proved there, whose working set is the server's from then on;
    # - a hydrate that may have replaced the Worker's working set keeps it: a
    #   failed transfer, which the Worker can report after its swap, one that
    #   timed out, and one whose session ended while it awaited its answer;
    # - one that left the working set as it was puts back the mark the start
    #   found: a refusal before the pull, a hydrate never sent because the
    #   Worker had no session, a wait that ran out before the hydrate was sent,
    #   and a stop that withdrew the start during the wait.
    server = _server(desired=DesiredState.RUNNING, on_worker=True)
    server.awaiting_hydrate = prior
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    clock = FakeClock(_NOW)
    at_wait: list[bool] = []

    def _during() -> None:
        row = uow.servers.by_id[server.id]
        at_wait.append(row.awaiting_hydrate)
        if stopped:
            # The stop's flip, commit and bump: the row keeps its Worker for the
            # final snapshot.
            row.desired_state = DesiredState.STOPPED
            row.updated_at = _NOW + dt.timedelta(seconds=1)
            start.stop_intents.bump(server.id, flipped_at=row.updated_at)

    reader = _Uploads(
        clock, takes=0 if settled else _BUDGET, settled=settled, during=_during
    )
    start = _start(uow, make_cp(), reader, clock)

    with contextlib.suppress(ServerError, WorkerUnavailableError):
        await start.redispatch_start(
            community_id=server.community_id, server_id=server.id
        )

    expected = prior if marked is _PRIOR else marked
    assert (at_wait, uow.servers.by_id[server.id].awaiting_hydrate) == (
        [True],
        expected,
    )


async def test_a_resent_start_puts_the_mark_back_when_its_adoption_is_not_written() -> (
    None
):
    # Issue #4024. The Worker refused the hydrate INVALID_STATE, so an instance of
    # the server runs there and its working set is the server's. Recording it
    # running clears the mark, but the monotonic guard drops that write when the
    # row was observed at or after its stamp: a Worker report the sink stamped on
    # receipt between the start's clock read and its write is enough. The
    # hydrate replaced nothing all the same, so the start puts back the mark it
    # found, and the instance's snapshots publish.
    server = _server(desired=DesiredState.RUNNING, on_worker=True)
    server.observed_at = _NOW
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    clock = FakeClock(_NOW)
    cp = _hydrate_answering(_hydrate_answers("instance_running"))()

    await _start(
        uow, cp, _Uploads(clock, takes=0, settled=True), clock
    ).redispatch_start(community_id=server.community_id, server_id=server.id)

    row = uow.servers.by_id[server.id]
    assert (row.observed_state, row.awaiting_hydrate) == (ObservedState.STOPPED, False)


@pytest.mark.parametrize(
    "newer", ["placed", "placed-and-stopped", "placed-and-being-stopped"]
)
async def test_a_resent_start_leaves_the_mark_of_a_newer_placement(newer: str) -> None:
    # Issue #4024. While the re-sent start waited, a stop withdrew its intent and
    # released the row, and a newer start placed the server on the same Worker,
    # whose hydrate has not landed. A second stop, of that newer start, may hold
    # the row too, its flip visible before it has bumped. The mark is the newer
    # placement's, so the re-sent start answers the conflict and leaves it: the
    # newer start's scratch may be older than the store.
    server = _server(desired=DesiredState.RUNNING, on_worker=True)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = FakeControlPlane(hydrate_timeout_seconds=_BUDGET)
    clock = FakeClock(_NOW)

    def _during() -> None:
        row = uow.servers.by_id[server.id]
        start.stop_intents.bump(server.id, flipped_at=_NOW + dt.timedelta(seconds=1))
        row.updated_at = _NOW + dt.timedelta(seconds=2)
        row.awaiting_hydrate = True
        if newer != "placed":
            row.desired_state = DesiredState.STOPPED
            row.updated_at = _NOW + dt.timedelta(seconds=3)
        if newer == "placed-and-stopped":
            start.stop_intents.bump(server.id, flipped_at=row.updated_at)

    start = _start(
        uow, cp, _Uploads(clock, takes=0, settled=True, during=_during), clock
    )

    with pytest.raises(LifecycleTransitionConflictError):
        await start.redispatch_start(
            community_id=server.community_id, server_id=server.id
        )

    assert cp.dispatched == []
    assert uow.servers.by_id[server.id].awaiting_hydrate is True
