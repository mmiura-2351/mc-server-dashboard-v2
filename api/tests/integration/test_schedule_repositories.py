"""Integration tests for the schedule repositories on PostgreSQL (issue #1835).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). The schema is created and torn down
per test via the real migrations so the adapters run against the documented
shape. A community + server are seeded through the existing adapters; schedules
and runs are round-tripped under the unit of work, and the delete cascades are
verified (a server's schedules go with it; a schedule's runs go with it).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId as CommunityCommunityId,
)
from mc_server_dashboard_api.community.domain.value_objects import CommunityName
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.servers.adapters.schedule_models import ScheduleModel
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.application.schedules import DeleteSchedule
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.schedule import (
    Cadence,
    Schedule,
    ScheduleAction,
    ScheduleId,
    ScheduleRun,
    ScheduleRunId,
    ScheduleRunOutcome,
    WarningStep,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import (
    FakeClock,
    FakeFileStore,
    FakeVersionValidator,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 7, 11, 12, 0, tzinfo=dt.timezone.utc)


async def _allow(_code: str) -> bool:
    """Allow-all authorizer: the write gate is not what these tests exercise."""

    return True


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


async def _seed_server(engine: AsyncEngine) -> uuid.UUID:
    community = Community(
        id=CommunityCommunityId(uuid.uuid4()),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    factory = create_session_factory(engine)
    async with CommunityUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.commit()
    server = await CreateServer(
        uow=ServersUnitOfWork(factory),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )(
        community_id=CommunityId(community.id.value),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        config={},
    )
    return server.id.value


def _schedule(
    server_id: uuid.UUID,
    *,
    name: str = "nightly",
    action: ScheduleAction = ScheduleAction.BACKUP,
    cadence: Cadence | None = None,
    timezone: str = "UTC",
    command: str | None = None,
    warning_steps: tuple[WarningStep, ...] = (),
    enabled: bool = False,
    next_run_at: dt.datetime | None = None,
) -> Schedule:
    return Schedule(
        id=ScheduleId.new(),
        server_id=ServerId(server_id),
        name=name,
        action=action,
        cadence=cadence or Cadence.from_interval(3600),
        enabled=enabled,
        created_at=_NOW,
        updated_at=_NOW,
        timezone=timezone,
        command=command,
        warning_steps=warning_steps,
        next_run_at=next_run_at,
        created_by=None,
    )


async def test_schedule_round_trips_every_field(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    schedule = _schedule(
        server_id,
        name="evening restart",
        action=ScheduleAction.RESTART,
        cadence=Cadence.from_cron("0 4 * * *"),
        timezone="Europe/Berlin",
        warning_steps=(
            WarningStep(offset_minutes=10, message="restart in 10 minutes"),
            WarningStep(offset_minutes=1, message="restart in 1 minute"),
        ),
        enabled=True,
        next_run_at=_NOW + dt.timedelta(hours=2),
    )

    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.add(schedule)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        fetched = await uow.schedules.get_by_id(schedule.id)
    assert fetched == schedule


async def test_command_payload_round_trips(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    schedule = _schedule(
        server_id,
        action=ScheduleAction.COMMAND,
        command="say the sun sets soon",
    )

    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.add(schedule)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        fetched = await uow.schedules.get_by_id(schedule.id)
    assert fetched is not None
    assert fetched.command == "say the sun sets soon"
    assert fetched.warning_steps == ()


async def test_list_for_server_orders_by_name(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    zulu = _schedule(server_id, name="zulu")
    alpha = _schedule(server_id, name="alpha")

    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.add(zulu)
        await uow.schedules.add(alpha)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        listed = await uow.schedules.list_for_server(ServerId(server_id))
    assert [s.name for s in listed] == ["alpha", "zulu"]


async def test_update_persists_mutable_fields(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    schedule = _schedule(server_id)

    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.add(schedule)
        await uow.commit()

    schedule.name = "hourly"
    schedule.cadence = Cadence.from_cron("0 * * * *")
    schedule.enabled = True
    schedule.next_run_at = _NOW + dt.timedelta(hours=1)
    schedule.last_run_at = _NOW
    schedule.updated_at = _NOW + dt.timedelta(minutes=5)
    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.update(schedule)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        fetched = await uow.schedules.get_by_id(schedule.id)
    assert fetched == schedule


async def test_delete_removes_schedule(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    schedule = _schedule(server_id)

    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.add(schedule)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.delete(schedule.id)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        assert await uow.schedules.get_by_id(schedule.id) is None


async def test_runs_round_trip_and_list_newest_first(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    schedule = _schedule(server_id)
    older = ScheduleRun(
        id=ScheduleRunId.new(),
        schedule_id=schedule.id,
        started_at=_NOW,
        finished_at=_NOW + dt.timedelta(seconds=4),
        outcome=ScheduleRunOutcome.SUCCESS,
        detail=None,
    )
    newer = ScheduleRun(
        id=ScheduleRunId.new(),
        schedule_id=schedule.id,
        started_at=_NOW + dt.timedelta(hours=1),
        finished_at=_NOW + dt.timedelta(hours=1, seconds=2),
        outcome=ScheduleRunOutcome.SKIPPED,
        detail="server not running",
    )

    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.add(schedule)
        await uow.schedule_runs.add(older)
        await uow.schedule_runs.add(newer)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        listed = await uow.schedule_runs.list_for_schedule(schedule.id)
    assert listed == [newer, older]


async def test_list_due_returns_only_enabled_past_due(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    due = _schedule(
        server_id, name="due", enabled=True, next_run_at=_NOW - dt.timedelta(minutes=1)
    )
    future = _schedule(
        server_id,
        name="future",
        enabled=True,
        next_run_at=_NOW + dt.timedelta(minutes=1),
    )
    disabled = _schedule(server_id, name="disabled", enabled=False, next_run_at=None)

    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.add(due)
        await uow.schedules.add(future)
        await uow.schedules.add(disabled)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        listed = await uow.schedules.list_due(_NOW)
    # Only the enabled, at-or-before-now row; the future and disabled ones excluded.
    assert [s.id for s in listed] == [due.id]

    # A row exactly at ``now`` is due (the ``<= now`` boundary).
    async with ServersUnitOfWork(factory) as uow:
        boundary = await uow.schedules.list_due(future.next_run_at)  # type: ignore[arg-type]
    assert {s.id for s in boundary} == {due.id, future.id}


async def test_list_warning_candidates_returns_upcoming_stop_restart(
    engine: AsyncEngine,
) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    until = _NOW + dt.timedelta(minutes=120)
    ahead = _schedule(
        server_id,
        name="ahead",
        action=ScheduleAction.STOP,
        enabled=True,
        next_run_at=_NOW + dt.timedelta(minutes=30),
    )
    restart = _schedule(
        server_id,
        name="restart",
        action=ScheduleAction.RESTART,
        enabled=True,
        next_run_at=until,  # exactly the horizon boundary is included
    )
    due = _schedule(
        server_id,
        name="due",
        action=ScheduleAction.STOP,
        enabled=True,
        next_run_at=_NOW,  # at-or-before now: the due poll's job, not a warning
    )
    beyond = _schedule(
        server_id,
        name="beyond",
        action=ScheduleAction.STOP,
        enabled=True,
        next_run_at=until + dt.timedelta(minutes=1),  # past the horizon
    )
    other_action = _schedule(
        server_id,
        name="other",
        action=ScheduleAction.BACKUP,
        enabled=True,
        next_run_at=_NOW + dt.timedelta(minutes=30),  # backup carries no warnings
    )
    disabled = _schedule(
        server_id,
        name="disabled",
        action=ScheduleAction.STOP,
        enabled=False,
        next_run_at=None,
    )

    async with ServersUnitOfWork(factory) as uow:
        for schedule in (ahead, restart, due, beyond, other_action, disabled):
            await uow.schedules.add(schedule)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        listed = await uow.schedules.list_warning_candidates(_NOW, until)
    # Only the enabled stop/restart rows strictly ahead of now and within the
    # horizon, ordered by next_run_at.
    assert [s.id for s in listed] == [ahead.id, restart.id]


async def _insert_corrupt_row(
    engine: AsyncEngine,
    *,
    server_id: uuid.UUID,
    name: str,
    action: ScheduleAction = ScheduleAction.BACKUP,
    next_run_at: dt.datetime,
) -> uuid.UUID:
    """Insert a schedule row the domain cannot hydrate (issue #2150 / #1856).

    An ``interval_seconds`` below the domain floor bypasses ``Cadence``
    validation only through a raw INSERT — a past migration bug or a manual DB
    edit. The row is enabled and due, so it would poll on every tick until
    quarantined.
    """
    factory = create_session_factory(engine)
    row_id = uuid.uuid4()
    async with factory() as session:
        session.add(
            ScheduleModel(
                id=row_id,
                server_id=server_id,
                name=name,
                action=action.value,
                payload={},
                cron=None,
                interval_seconds=0,  # below the floor: unhydratable
                timezone="UTC",
                enabled=True,
                next_run_at=next_run_at,
                last_run_at=None,
                created_by=None,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        await session.commit()
    return row_id


async def test_list_due_quarantines_a_corrupt_row(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """A corrupt due row is disabled once, dropping out of the poll (#2150).

    Left enabled, a row that fails hydration would stay perpetually due: the
    same warning every tick and a schedule that silently never fires. The poll
    quarantines it (``enabled = false``) so it leaves both list methods.
    """
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    good = _schedule(
        server_id,
        name="good",
        enabled=True,
        next_run_at=_NOW - dt.timedelta(minutes=1),
    )
    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.add(good)
        await uow.commit()
    bad_id = await _insert_corrupt_row(
        engine,
        server_id=server_id,
        name="corrupt",
        next_run_at=_NOW - dt.timedelta(minutes=1),
    )

    # The runner's due poll: read, then commit the poll transaction.
    with caplog.at_level(logging.ERROR):
        async with ServersUnitOfWork(factory) as uow:
            listed = await uow.schedules.list_due(_NOW)
            await uow.commit()

    # The corrupt row is skipped; the valid row is still returned.
    assert [s.id for s in listed] == [good.id]
    # A distinct quarantine log identifies the row, logged exactly once.
    quarantined = [r for r in caplog.records if "quarantined" in r.getMessage()]
    assert len(quarantined) == 1
    assert str(bad_id) in quarantined[0].getMessage()

    # The corrupt row is now disabled with next_run_at cleared (the disabled
    # invariant), so it no longer matches the enabled/due predicate.
    async with factory() as session:
        row = await session.get(ScheduleModel, bad_id)
    assert row is not None
    assert row.enabled is False
    assert row.next_run_at is None

    # A second poll returns only the valid row and neither re-disables nor
    # re-logs — the quarantine is self-limiting.
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        async with ServersUnitOfWork(factory) as uow:
            second = await uow.schedules.list_due(_NOW)
            await uow.commit()
    assert [s.id for s in second] == [good.id]
    assert not [r for r in caplog.records if "quarantined" in r.getMessage()]


async def test_list_warning_candidates_quarantines_a_corrupt_row(
    engine: AsyncEngine,
) -> None:
    """The warning look-ahead also disables a corrupt row it cannot hydrate (#2150)."""
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    until = _NOW + dt.timedelta(minutes=120)
    bad_id = await _insert_corrupt_row(
        engine,
        server_id=server_id,
        name="corrupt-warn",
        action=ScheduleAction.STOP,
        next_run_at=_NOW + dt.timedelta(minutes=30),
    )

    async with ServersUnitOfWork(factory) as uow:
        listed = await uow.schedules.list_warning_candidates(_NOW, until)
        await uow.commit()
    assert listed == []

    async with factory() as session:
        row = await session.get(ScheduleModel, bad_id)
    assert row is not None
    assert row.enabled is False


async def test_list_for_server_skips_a_corrupt_row(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """The admin/UI listing omits a row it cannot hydrate instead of raising (#2712).

    The quarantine log (#2150) tells the operator to correct or delete the row,
    so the listing that would show it must not fail on it. The corrupt row is
    skipped with a WARN naming its id; unlike the runner's polls this read path
    never commits, so it does not quarantine — the row is left as it is.
    """
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    good = _schedule(server_id, name="alpha")
    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.add(good)
        await uow.commit()
    bad_id = await _insert_corrupt_row(
        engine, server_id=server_id, name="zulu-corrupt", next_run_at=_NOW
    )

    with caplog.at_level(logging.WARNING):
        async with ServersUnitOfWork(factory) as uow:
            listed = await uow.schedules.list_for_server(ServerId(server_id))

    assert [s.id for s in listed] == [good.id]
    skipped = [r for r in caplog.records if "failed to hydrate" in r.getMessage()]
    assert len(skipped) == 1
    assert str(bad_id) in skipped[0].getMessage()

    async with factory() as session:
        row = await session.get(ScheduleModel, bad_id)
    assert row is not None
    assert row.enabled is True


async def test_get_by_id_returns_none_for_a_corrupt_row(engine: AsyncEngine) -> None:
    """A row the domain cannot hydrate reads as absent (404), not as a 500 (#2712)."""
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    bad_id = await _insert_corrupt_row(
        engine, server_id=server_id, name="corrupt", next_run_at=_NOW
    )

    async with ServersUnitOfWork(factory) as uow:
        assert await uow.schedules.get_by_id(ScheduleId(bad_id)) is None


async def test_delete_use_case_removes_a_corrupt_row(engine: AsyncEngine) -> None:
    """The operator can delete the row the quarantine log names (#2712).

    ``DeleteSchedule`` resolves existence, ownership and action through the
    un-hydrated ``get_ref``, so deleting never depends on the domain being able
    to load the row — delete is the one action the quarantine log asks for.
    """
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    async with ServersUnitOfWork(factory) as uow:
        server = await uow.servers.get_by_id(ServerId(server_id))
    assert server is not None
    bad_id = await _insert_corrupt_row(
        engine, server_id=server_id, name="corrupt", next_run_at=_NOW
    )

    await DeleteSchedule(uow=ServersUnitOfWork(factory))(
        community_id=server.community_id,
        server_id=ServerId(server_id),
        schedule_id=ScheduleId(bad_id),
        authorize=_allow,
    )

    async with factory() as session:
        assert await session.get(ScheduleModel, bad_id) is None


async def test_advance_run_state_updates_only_bookkeeping(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    schedule = _schedule(
        server_id, enabled=True, next_run_at=_NOW - dt.timedelta(minutes=1)
    )

    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.add(schedule)
        await uow.commit()

    next_run = _NOW + dt.timedelta(hours=1)
    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.advance_run_state(
            schedule.id,
            fired_occurrence=schedule.next_run_at,  # type: ignore[arg-type]
            next_run_at=next_run,
            last_run_at=_NOW,
        )
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        fetched = await uow.schedules.get_by_id(schedule.id)
    assert fetched is not None
    assert fetched.next_run_at == next_run
    assert fetched.last_run_at == _NOW
    # Everything else is untouched (only the bookkeeping columns are written).
    assert fetched.name == schedule.name
    assert fetched.enabled is True
    assert fetched.updated_at == schedule.updated_at


async def test_advance_run_state_skips_a_disabled_schedule(
    engine: AsyncEngine,
) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    schedule = _schedule(server_id, enabled=False, next_run_at=None)

    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.add(schedule)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.advance_run_state(
            schedule.id,
            fired_occurrence=_NOW,  # any value; the row is disabled so it won't match
            next_run_at=_NOW + dt.timedelta(hours=1),
            last_run_at=_NOW,
        )
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        fetched = await uow.schedules.get_by_id(schedule.id)
    # The guarded UPDATE matched no row: the disabled schedule is not resurrected.
    assert fetched is not None
    assert fetched.enabled is False
    assert fetched.next_run_at is None
    assert fetched.last_run_at is None


async def test_advance_run_state_skips_when_next_run_at_moved_concurrently(
    engine: AsyncEngine,
) -> None:
    """CAS guard: a concurrent PATCH that recomputed next_run_at wins (#1963)."""
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    original_next = _NOW - dt.timedelta(minutes=1)
    schedule = _schedule(server_id, enabled=True, next_run_at=original_next)

    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.add(schedule)
        await uow.commit()

    # Simulate a concurrent PATCH that changed the cadence and recomputed
    # next_run_at to a new value (T1) while the runner held the old one (T0).
    patched_next = _NOW + dt.timedelta(hours=2)
    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.schedules.get_by_id(schedule.id)
        assert loaded is not None
        loaded.next_run_at = patched_next
        await uow.schedules.update(loaded)
        await uow.commit()

    # The runner's advance passes fired_occurrence=T0 (the stale value it read
    # at list_due time); the CAS guard should reject the UPDATE.
    stale_fired = original_next
    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.advance_run_state(
            schedule.id,
            fired_occurrence=stale_fired,
            next_run_at=_NOW + dt.timedelta(hours=1),
            last_run_at=_NOW,
        )
        await uow.commit()

    # The concurrent edit's value survives; the runner's stale advance is a no-op.
    async with ServersUnitOfWork(factory) as uow:
        fetched = await uow.schedules.get_by_id(schedule.id)
    assert fetched is not None
    assert fetched.next_run_at == patched_next
    assert fetched.last_run_at is None


async def test_prune_keeps_only_the_newest_runs(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    schedule = _schedule(server_id)
    runs = [
        ScheduleRun(
            id=ScheduleRunId.new(),
            schedule_id=schedule.id,
            started_at=_NOW + dt.timedelta(minutes=i),
            finished_at=_NOW + dt.timedelta(minutes=i, seconds=1),
            outcome=ScheduleRunOutcome.SUCCESS,
            detail=None,
        )
        for i in range(5)
    ]

    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.add(schedule)
        for run in runs:
            await uow.schedule_runs.add(run)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        await uow.schedule_runs.prune_for_schedule(schedule.id, keep=2)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        remaining = await uow.schedule_runs.list_for_schedule(schedule.id)
    # The two newest survive (started_at descending); the three oldest are gone.
    assert [r.id for r in remaining] == [runs[4].id, runs[3].id]


async def test_deleting_server_cascades_to_schedules_and_runs(
    engine: AsyncEngine,
) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    schedule = _schedule(server_id)
    run = ScheduleRun(
        id=ScheduleRunId.new(),
        schedule_id=schedule.id,
        started_at=_NOW,
        finished_at=_NOW,
        outcome=ScheduleRunOutcome.FAILURE,
        detail="dispatch_failed",
    )

    async with ServersUnitOfWork(factory) as uow:
        await uow.schedules.add(schedule)
        await uow.schedule_runs.add(run)
        await uow.commit()

    # Delete the server row directly; the FK cascades remove the schedule and,
    # through it, the run.
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM server WHERE id = :id"), {"id": server_id})
    async with engine.connect() as conn:
        schedules = (
            await conn.execute(text("SELECT count(*) FROM schedule"))
        ).scalar_one()
        runs = (
            await conn.execute(text("SELECT count(*) FROM schedule_run"))
        ).scalar_one()
    assert schedules == 0
    assert runs == 0
