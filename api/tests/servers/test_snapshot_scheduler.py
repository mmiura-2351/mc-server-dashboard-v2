"""Use-case tests for the periodic snapshot scheduler (FR-DATA-5/7).

Drives :class:`RunSnapshotCadenceTick` against in-memory fakes with a faked
clock: due servers whose worker is connected are dispatched a snapshot; a
disconnected worker is skipped; a failed dispatch is retried on the next tick;
and the due math honours the default / override / floor.
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
from tests.servers.fakes import (
    FakeClock,
    FakeControlPlane,
    FakeStoreGenerationReader,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_WORKER = WorkerId(uuid.uuid4())


def _running_server(
    *,
    server_id: ServerId | None = None,
    worker: WorkerId | None = _WORKER,
    config: dict[str, object] | None = None,
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
        observed_state=ObservedState.RUNNING,
        observed_at=None,
        assigned_worker_id=worker,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _scheduler(
    uow: FakeUnitOfWork,
    cp: FakeControlPlane,
    clock: FakeClock,
    *,
    store_generation: FakeStoreGenerationReader | None = None,
) -> RunSnapshotCadenceTick:
    return RunSnapshotCadenceTick(
        uow=uow,
        control_plane=cp,
        clock=clock,
        store_generation=store_generation or FakeStoreGenerationReader(),
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


async def test_failed_dispatch_is_retried_next_tick() -> None:
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
    await scheduler.tick()  # due, dispatched, fails -> next_due unchanged
    assert len(cp.dispatched) == 1
    clock.set(_NOW + dt.timedelta(seconds=3700))
    await scheduler.tick()  # still due (no success recorded) -> retried
    assert len(cp.dispatched) == 2


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


# --- the periodic snapshot refreshes the held-working-set inventory (#2477) ---


async def test_periodic_snapshot_records_the_published_generation_as_held() -> None:
    # A snapshot published by the assigned Worker proves the store's current working
    # set came FROM that Worker's scratch, so the scratch is at least as fresh as the
    # store. Record it as held (issue #2477) — otherwise every periodic snapshot
    # advances the store past what the held inventory says, and the next restart falls
    # back to a full hydrate plus the long reconciler grace.
    uow = FakeUnitOfWork()
    server = _running_server()
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    scheduler = _scheduler(
        uow,
        cp,
        clock,
        store_generation=FakeStoreGenerationReader(
            generation=7, publisher=str(_WORKER.value)
        ),
    )
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(seconds=3600))

    await scheduler.tick()

    assert cp.recorded_held == [(_WORKER, server.id, 7)]


async def test_periodic_snapshot_records_nothing_when_another_publisher_won() -> None:
    # The store's current generation was published by someone ELSE (an at-rest edit, a
    # restore, or another Worker) after ours committed: that generation did NOT come
    # from this Worker's scratch, so claiming it as held would let a later start skip a
    # hydrate it needs and boot the pre-edit world. Record nothing — the entry stays
    # stale, so the next start hydrates (issue #2477).
    uow = FakeUnitOfWork()
    server = _running_server()
    uow.servers.seed(server)
    cp = FakeControlPlane()
    clock = FakeClock(_NOW)
    scheduler = _scheduler(
        uow,
        cp,
        clock,
        store_generation=FakeStoreGenerationReader(generation=8, publisher="api-edit"),
    )
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(seconds=3600))

    await scheduler.tick()

    assert [k for k, _, _ in cp.dispatched] == ["snapshot"]
    assert cp.recorded_held == []


async def test_failed_periodic_snapshot_records_nothing_as_held() -> None:
    # A snapshot that never published leaves the store where it was, and the Worker's
    # scratch is whatever it already was — there is nothing new to record (#2477).
    uow = FakeUnitOfWork()
    server = _running_server()
    uow.servers.seed(server)
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.TRANSFER_FAILED, message="boom")
    )
    clock = FakeClock(_NOW)
    scheduler = _scheduler(
        uow,
        cp,
        clock,
        store_generation=FakeStoreGenerationReader(
            generation=7, publisher=str(_WORKER.value)
        ),
    )
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(seconds=3600))

    await scheduler.tick()

    assert cp.recorded_held == []


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
