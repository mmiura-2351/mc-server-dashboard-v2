"""Use-case tests for the periodic snapshot scheduler (FR-DATA-5/7).

Drives :class:`RunSnapshotCadenceTick` against in-memory fakes with a faked
clock: due servers whose worker is connected are dispatched a snapshot; a
disconnected worker is skipped; a failed dispatch advances next-due like a
successful one, so the retry waits one interval rather than one tick; and the due
math honours the default / override / floor.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

import pytest

from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    RunSnapshotCadenceTick,
    SnapshotServer,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    CommandDispatchError,
    ServerNotFoundError,
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
from tests.servers.contract_table import worker_status
from tests.servers.fakes import FakeClock, FakeControlPlane, FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_WORKER = WorkerId(uuid.uuid4())


def _running_server(
    *,
    server_id: ServerId | None = None,
    worker: WorkerId | None = _WORKER,
    config: dict[str, object] | None = None,
    observed: ObservedState = ObservedState.RUNNING,
) -> Server:
    return Server(
        id=server_id or ServerId.new(),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config=config or {},
        desired_state=DesiredState.RUNNING,
        observed_state=observed,
        observed_at=None,
        assigned_worker_id=worker,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _scheduler(
    uow: FakeUnitOfWork,
    cp: FakeControlPlane,
    clock: FakeClock,
) -> RunSnapshotCadenceTick:
    return RunSnapshotCadenceTick(
        uow=uow,
        control_plane=cp,
        clock=clock,
        default_interval_seconds=3600,
        min_interval_seconds=300,
    )


async def test_first_tick_does_not_snapshot_immediately() -> None:
    # A freshly observed running server is scheduled for its first snapshot a
    # jitter offset into the future, not on the same tick (herd guard).
    uow = FakeUnitOfWork()
    server = _running_server()
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    scheduler = _scheduler(uow, cp, clock)
    await scheduler.tick()
    assert cp.dispatched == []


async def test_due_server_is_snapshotted() -> None:
    uow = FakeUnitOfWork()
    server = _running_server()
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    scheduler = _scheduler(uow, cp, clock)
    await scheduler.tick()  # schedules the first due instant
    clock.set(_NOW + dt.timedelta(seconds=3600))  # well past interval + jitter
    await scheduler.tick()
    assert [k for k, _, _ in cp.dispatched] == ["snapshot"]
    assert cp.dispatched[0][2] == server.id


async def test_disconnected_worker_is_skipped() -> None:
    uow = FakeUnitOfWork()
    server = _running_server()
    uow.servers.seed(server)
    cp = FakeControlPlane(connected={_WORKER: False})
    clock = FakeClock(_NOW)
    scheduler = _scheduler(uow, cp, clock)
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(seconds=3600))
    await scheduler.tick()
    assert cp.dispatched == []


async def test_failed_dispatch_advances_next_due() -> None:
    # Issue #2485: a failed dispatch advances next-due exactly as a successful one
    # does, so a persistently failing server is retried at its own interval. Leaving
    # next-due in the past re-dispatched it on every tick instead, and the tick
    # period is the interval floor (300s), so the retry — and its WARN — ran at that
    # cadence forever however long the failure lasted.
    uow = FakeUnitOfWork()
    server = _running_server()
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.TRANSFER_FAILED, message="boom")
    )
    clock = FakeClock(_NOW)
    scheduler = _scheduler(uow, cp, clock)
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(seconds=3600))
    await scheduler.tick()  # due, dispatched, fails -> next-due advances anyway
    assert len(cp.dispatched) == 1
    clock.set(_NOW + dt.timedelta(seconds=3700))  # < one interval after the failure
    await scheduler.tick()  # not due again yet -> no re-dispatch
    assert len(cp.dispatched) == 1
    clock.set(_NOW + dt.timedelta(seconds=3600 + 4000))  # > interval after failure
    await scheduler.tick()  # retried on its own interval
    assert len(cp.dispatched) == 2


async def test_unreachable_worker_advances_next_due() -> None:
    # Issue #2485 for the transport failure: the connectivity gate passed, so the
    # trigger WAS attempted (and WARNed) — it just did not reach the Worker. Same
    # rule as any other failed dispatch: retry on the interval, not on the tick.
    uow = FakeUnitOfWork()
    server = _running_server()
    uow.servers.seed(server)
    cp = FakeControlPlane(unavailable_kinds={"snapshot"})
    clock = FakeClock(_NOW)
    scheduler = _scheduler(uow, cp, clock)
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(seconds=3600))
    await scheduler.tick()  # due, dispatched, unreachable
    assert len(cp.dispatched) == 1
    clock.set(_NOW + dt.timedelta(seconds=3700))  # < one interval after the failure
    await scheduler.tick()
    assert len(cp.dispatched) == 1


async def test_success_reschedules_one_interval_out() -> None:
    uow = FakeUnitOfWork()
    server = _running_server()
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    scheduler = _scheduler(uow, cp, clock)
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(seconds=3600))
    await scheduler.tick()  # snapshot taken
    assert len(cp.dispatched) == 1
    clock.set(_NOW + dt.timedelta(seconds=3700))  # < one interval after success
    await scheduler.tick()  # not yet due again
    assert len(cp.dispatched) == 1
    clock.set(_NOW + dt.timedelta(seconds=3600 + 4000))  # > interval after success
    await scheduler.tick()
    assert len(cp.dispatched) == 2


async def test_override_below_default_snapshots_sooner() -> None:
    # An override of 300s (the floor) makes the server due far sooner than the
    # 3600s default would.
    uow = FakeUnitOfWork()
    server = _running_server(config={"snapshot_interval_seconds": 300})
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    scheduler = _scheduler(uow, cp, clock)
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(seconds=400))  # past 300 + jitter(<=30), < 3600
    await scheduler.tick()
    assert len(cp.dispatched) == 1


# The Worker's working_set_absent refusal message, verbatim (issue #1713,
# worker/internal/application/instancemanager/instancemanager.go handleSnapshot;
# the guard's predicate is the working set's content since issue #2813, which is
# what added the emptied-out-of-band cause). The API discriminator matches the
# "working dir absent" phrase inside it.
_WORKING_SET_ABSENT_MESSAGE = (
    "instancemanager: snapshot refused: working dir absent (no working set held "
    "for this id: scratch already GC'd after a published final snapshot, emptied "
    "out of band, or never hydrated)"
)


async def test_crashed_server_is_still_snapshotted() -> None:
    # Issue #2480: the candidate set is desired-only, and dispatching to a crashed
    # member of it is the DECISION, not an accident. The Worker dropped the crashed
    # instance from its map, so the trigger takes the at-rest path over the retained
    # directory and publishes the crash-time world — the only path that durably
    # captures it under desired=running (the reconciler's automatic redispatch_start
    # otherwise hydrates over the crash-window scratch and it is swept).
    uow = FakeUnitOfWork()
    server = _running_server(observed=ObservedState.CRASHED)
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    scheduler = _scheduler(uow, cp, clock)
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(seconds=3600))
    await scheduler.tick()
    assert [sid for _, _, sid in cp.dispatched] == [server.id]


async def test_working_set_absent_refusal_advances_next_due() -> None:
    # Issue #2480: the at-rest capture GC's the scratch after publishing, so every
    # later dispatch for the same id answers the working_set_absent refusal. There
    # is nothing left to capture, so that answer advances next-due like a success.
    uow = FakeUnitOfWork()
    server = _running_server(observed=ObservedState.CRASHED)
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcome=CommandOutcome(
            status=worker_status("SnapshotTrigger", "working_set_absent"),
            message=_WORKING_SET_ABSENT_MESSAGE,
        )
    )
    clock = FakeClock(_NOW)
    scheduler = _scheduler(uow, cp, clock)
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(seconds=3600))
    await scheduler.tick()  # due, dispatched, refused: nothing to capture
    assert len(cp.dispatched) == 1
    clock.set(_NOW + dt.timedelta(seconds=3700))  # well under one interval later
    await scheduler.tick()
    assert len(cp.dispatched) == 1


async def test_working_set_absent_refusal_is_not_logged_as_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Since issue #2485 every outcome advances next-due, so the discriminator's
    # whole remaining effect is how the outcome is logged: nothing was lost, so the
    # refusal must not WARN. Pinned here (with the guard below) so the
    # discriminator cannot quietly become dead code.
    uow = FakeUnitOfWork()
    server = _running_server(observed=ObservedState.CRASHED)
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcome=CommandOutcome(
            status=worker_status("SnapshotTrigger", "working_set_absent"),
            message=_WORKING_SET_ABSENT_MESSAGE,
        )
    )
    clock = FakeClock(_NOW)
    scheduler = _scheduler(uow, cp, clock)
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(seconds=3600))

    with caplog.at_level(logging.INFO):
        await scheduler.tick()

    assert [r.levelno for r in caplog.records] == [logging.INFO]


async def test_other_server_not_found_is_logged_as_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Guard for issue #1790's rule at this call site: the SERVER_NOT_FOUND code
    # alone must not be read as "nothing to capture" — only the pinned
    # working_set_absent phrase may be. Any other SERVER_NOT_FOUND is a genuine
    # failure, and since issue #2485 that shows in the log level rather than in the
    # retry cadence.
    uow = FakeUnitOfWork()
    server = _running_server()
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcome=CommandOutcome(
            status=CommandStatus.SERVER_NOT_FOUND, message="instancemanager: nope"
        )
    )
    clock = FakeClock(_NOW)
    scheduler = _scheduler(uow, cp, clock)
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(seconds=3600))

    with caplog.at_level(logging.INFO):
        await scheduler.tick()

    assert [r.levelno for r in caplog.records] == [logging.WARNING]


async def test_only_running_assigned_servers_are_considered() -> None:
    # A stopped server and a running-but-unassigned server are not snapshotted.
    uow = FakeUnitOfWork()
    running = _running_server()
    uow.servers.seed(running)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    scheduler = _scheduler(uow, cp, clock)
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(seconds=3600))
    await scheduler.tick()
    assert {sid for _, _, sid in cp.dispatched} == {running.id}


# --- on-demand snapshot hook (SnapshotServer) ------------------------------


async def test_on_demand_snapshot_dispatches() -> None:
    uow = FakeUnitOfWork()
    server = _running_server()
    uow.servers.seed(server)
    cp = FakeControlPlane()
    result = await SnapshotServer(uow=uow, control_plane=cp)(
        community_id=server.community_id, server_id=server.id
    )
    assert result.id == server.id
    assert [k for k, _, _ in cp.dispatched] == ["snapshot"]


async def test_on_demand_snapshot_unknown_server_is_not_found() -> None:
    uow = FakeUnitOfWork()
    cp = FakeControlPlane()
    with pytest.raises(ServerNotFoundError):
        await SnapshotServer(uow=uow, control_plane=cp)(
            community_id=CommunityId(uuid.uuid4()), server_id=ServerId.new()
        )


async def test_on_demand_snapshot_failed_dispatch_raises() -> None:
    uow = FakeUnitOfWork()
    server = _running_server()
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.TRANSFER_FAILED, message="boom")
    )
    with pytest.raises(CommandDispatchError):
        await SnapshotServer(uow=uow, control_plane=cp)(
            community_id=server.community_id, server_id=server.id
        )


async def test_on_demand_snapshot_over_failed_stop_orphan_is_worker_busy() -> None:
    # A manual backup taken during the failed-stop-orphan window (issue #2471):
    # the row still reads observed=running, so CreateBackup takes the running path
    # and dispatches this SnapshotTrigger, which the Worker refuses because it
    # holds the orphan. Under issue #2476 that refusal is BUSY — the converger is
    # working the orphan, so the snapshot succeeds once it settles — and the
    # sanitized reason carries the retryable-ness to the client instead of the
    # unclassified ``command_failed`` catch-all.
    uow = FakeUnitOfWork()
    server = _running_server()
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcome=CommandOutcome(
            status=worker_status("SnapshotTrigger", "orphan_pending"),
            message="instancemanager: server has a failed-stop orphan pending "
            "termination",
        )
    )

    with pytest.raises(CommandDispatchError) as excinfo:
        await SnapshotServer(uow=uow, control_plane=cp)(
            community_id=server.community_id, server_id=server.id
        )

    assert excinfo.value.reason == "worker_busy"


async def test_on_demand_snapshot_failure_logs_warning_with_server_and_kind(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A failed on-demand snapshot dispatch turns into a CommandDispatchError; the
    # Worker's message is logged at WARN with server_id and command kind context
    # so the failure is diagnosable, while the raw message stays out of the HTTP
    # body (issue #200).
    uow = FakeUnitOfWork()
    server = _running_server()
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.TRANSFER_FAILED, message="boom")
    )

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(CommandDispatchError),
    ):
        await SnapshotServer(uow=uow, control_plane=cp)(
            community_id=server.community_id, server_id=server.id
        )

    record = next(r for r in caplog.records if r.levelno == logging.WARNING)
    message = record.getMessage()
    assert "boom" in message
    assert "SnapshotServer" in message
    assert str(server.id.value) in message
