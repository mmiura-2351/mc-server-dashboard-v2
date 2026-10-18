"""A schedule deleted while the runner executes it (#3314).

The runner reads the due schedules, dispatches each one's action, and then
records the run. A delete landing anywhere in that sequence used to make the
run-record insert fail its foreign key, logged as a generic tick error with a
traceback, and the action already dispatched went unaudited.

The runner now claims a schedule under its row lock before dispatching, and
records the run under the row lock again; neither lock is held across the
dispatch. These tests pin each order against a real delete on another
connection:

* the delete commits before the claim -- the action is never dispatched;
* the delete commits during the dispatch -- the run is audited, not recorded;
* the delete arrives while the run is being recorded -- it waits, and the run
  it then cascades away was recorded and audited.

The retry path goes through the same claim and record, and one schedule's
failure leaves the other schedules' work in the same tick committed.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (a real PostgreSQL); skipped
otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mc_server_dashboard_api.audit.domain.operations import SCHEDULE_RUN
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.servers.adapters.cronsim_next_run_calculator import (
    CronsimNextRunCalculator,
)
from mc_server_dashboard_api.servers.adapters.schedule_repository import (
    SqlAlchemyScheduleRepository,
    SqlAlchemyScheduleRunRepository,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.servers.application.schedule_runner import (
    ActionResult,
    ExecuteScheduleAction,
    RunScheduleTick,
)
from mc_server_dashboard_api.servers.application.schedules import DeleteSchedule
from mc_server_dashboard_api.servers.domain.schedule import (
    ScheduleAction,
    ScheduleId,
    ScheduleRun,
    ScheduleRunOutcome,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from tests.audit.fakes import RecordingAuditRecorder
from tests.integration.races import await_settled, race_database
from tests.servers.fakes import FakeClock, FakeServerNotifier

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 7, 11, 12, 0, 30, tzinfo=dt.timezone.utc)
_RUNNER_LOG = "mc_server_dashboard_api.servers.application.schedule_runner"


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    async with race_database(_DB_URL) as eng:
        yield eng


class _Pause:
    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.resume = asyncio.Event()

    async def hold(self) -> None:
        self.reached.set()
        await self.resume.wait()


class _PausingScheduleRepository(SqlAlchemyScheduleRepository):
    """Holds a delete after its DELETE ran, before its transaction commits."""

    def __init__(self, session: AsyncSession, pause: _Pause) -> None:
        super().__init__(session)
        self._pause = pause

    async def delete(self, schedule_id: ScheduleId) -> None:
        await super().delete(schedule_id)
        await self._pause.hold()


class _PausingRunRepository(SqlAlchemyScheduleRunRepository):
    """Holds the run record after its row was staged, before it commits."""

    def __init__(self, session: AsyncSession, pause: _Pause) -> None:
        super().__init__(session)
        self._pause = pause

    async def add(self, run: ScheduleRun) -> None:
        await super().add(run)
        await self._pause.hold()


class _UnitOfWork(SqlAlchemyUnitOfWork):
    """Reports its latest transaction's backend pid; optionally pauses a write."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        pause_delete: _Pause | None = None,
        pause_run_add: _Pause | None = None,
    ) -> None:
        super().__init__(factory)
        self.pid: int | None = None
        self._pause_delete = pause_delete
        self._pause_run_add = pause_run_add

    async def __aenter__(self) -> _UnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        self.pid = (
            await self._session.execute(text("SELECT pg_backend_pid()"))
        ).scalar_one()
        if self._pause_delete is not None:
            self.schedules = _PausingScheduleRepository(
                self._session, self._pause_delete
            )
        if self._pause_run_add is not None:
            self.schedule_runs = _PausingRunRepository(
                self._session, self._pause_run_add
            )
        return self


class _Execute:
    """Stands in for :class:`ExecuteScheduleAction`, recording each dispatch."""

    def __init__(
        self,
        community_id: CommunityId,
        *,
        outcome: ScheduleRunOutcome = ScheduleRunOutcome.SUCCESS,
        on_dispatch: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.community_id = community_id
        self.outcome = outcome
        self.on_dispatch = on_dispatch
        self.dispatched: list[str | None] = []

    async def __call__(
        self,
        *,
        server_id: ServerId,
        action: ScheduleAction,
        command: str | None,
        only_when_running: bool | None,
    ) -> ActionResult:
        self.dispatched.append(command)
        if self.on_dispatch is not None:
            await self.on_dispatch()
        # A NUL byte is refused by PostgreSQL text, so the run row of a
        # ``poison`` command fails to write: one schedule's own DB failure.
        detail = "bad\x00detail" if command == "poison" else None
        return ActionResult(self.outcome, detail, self.community_id)


async def _allow(_code: str) -> bool:
    return True


class _World:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.factory = create_session_factory(engine)
        self.community_id = CommunityId(uuid.uuid4())
        self.clock = FakeClock(_NOW)
        self.audit = RecordingAuditRecorder()

    async def seed_server(self) -> ServerId:
        server_id = ServerId(uuid.uuid4())
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO community (id, name, created_at, updated_at) "
                    "VALUES (:id, 'guild', now(), now()) ON CONFLICT DO NOTHING"
                ),
                {"id": self.community_id.value},
            )
            await conn.execute(
                text(
                    "INSERT INTO server "
                    "(id, community_id, name, mc_edition, mc_version, server_type, "
                    "config, slug, desired_state, observed_state, "
                    "created_at, updated_at) VALUES "
                    "(:id, :cid, :name, 'java', '1.21', 'vanilla', "
                    "'{}'::jsonb, :slug, 'stopped', 'stopped', now(), now())"
                ),
                {
                    "id": server_id.value,
                    "cid": self.community_id.value,
                    "name": f"srv-{str(server_id.value)[:8]}",
                    "slug": f"srv-{str(server_id.value)[:8]}-00",
                },
            )
        return server_id

    async def seed_schedule(
        self,
        server_id: ServerId,
        *,
        action: ScheduleAction = ScheduleAction.COMMAND,
        command: str = "say hi",
        due: dt.datetime = _NOW - dt.timedelta(seconds=10),
    ) -> ScheduleId:
        schedule_id = ScheduleId.new()
        payload = (
            '{"only_when_running": false}'
            if action is ScheduleAction.BACKUP
            else '{"command": "%s"}' % command
        )
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO schedule (id, server_id, name, action, payload, "
                    "interval_seconds, timezone, enabled, next_run_at, created_at, "
                    "updated_at) VALUES (:id, :sid, :name, :action, "
                    "CAST(:payload AS jsonb), 3600, 'UTC', true, :due, now(), now())"
                ),
                {
                    "id": schedule_id.value,
                    "sid": server_id.value,
                    "name": f"s-{str(schedule_id.value)[:8]}",
                    "action": action.value,
                    "payload": payload,
                    "due": due,
                },
            )
        return schedule_id

    def runner(
        self, execute: _Execute, uow: SqlAlchemyUnitOfWork | None = None
    ) -> RunScheduleTick:
        return RunScheduleTick(
            uow=uow or SqlAlchemyUnitOfWork(self.factory),
            make_execute=lambda: cast(ExecuteScheduleAction, execute),
            send_command=None,  # type: ignore[arg-type]  # no warnings here
            calculator=CronsimNextRunCalculator(),
            audit=self.audit,
            notifier=FakeServerNotifier(),
            clock=self.clock,
            backup_retry_delay=dt.timedelta(minutes=1),
        )

    async def delete(
        self, server_id: ServerId, schedule_id: ScheduleId, uow: SqlAlchemyUnitOfWork
    ) -> None:
        await DeleteSchedule(uow=uow)(
            community_id=self.community_id,
            server_id=server_id,
            schedule_id=schedule_id,
            authorize=_allow,
        )

    async def scalar(self, sql: str, **params: object) -> object:
        async with self.engine.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar_one()

    def audited(self, schedule_id: ScheduleId) -> bool:
        return any(
            event.operation == SCHEDULE_RUN and event.target_id == schedule_id.value
            for event in self.audit.events
        )


def _errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def _infos_naming(
    caplog: pytest.LogCaptureFixture, schedule_id: ScheduleId
) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _RUNNER_LOG
        and r.levelno == logging.INFO
        and str(schedule_id.value) in r.getMessage()
    ]


async def test_delete_committing_before_the_claim_is_never_dispatched(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    world = _World(engine)
    server_id = await world.seed_server()
    schedule_id = await world.seed_schedule(server_id)
    pause = _Pause()
    deletion = asyncio.create_task(
        world.delete(
            server_id, schedule_id, _UnitOfWork(world.factory, pause_delete=pause)
        )
    )
    await pause.reached.wait()
    execute = _Execute(world.community_id)
    runner_uow = _UnitOfWork(world.factory)
    tick = asyncio.create_task(world.runner(execute, runner_uow).tick())

    # The poll still reads the row (the delete is uncommitted); the claim waits
    # for the delete instead of dispatching beside it.
    await await_settled(engine, tick, lambda: runner_uow.pid)
    assert not tick.done()
    pause.resume.set()
    await deletion
    await tick

    assert execute.dispatched == []
    assert _errors(caplog) == []
    assert len(_infos_naming(caplog, schedule_id)) == 1
    assert await world.scalar("SELECT count(*) FROM schedule_run") == 0


async def test_delete_committing_during_the_dispatch_audits_the_run(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    world = _World(engine)
    server_id = await world.seed_server()
    schedule_id = await world.seed_schedule(server_id)

    async def delete_now() -> None:
        await world.delete(server_id, schedule_id, SqlAlchemyUnitOfWork(world.factory))

    execute = _Execute(world.community_id, on_dispatch=delete_now)

    await world.runner(execute).tick()

    # The action ran and is audited; there is no schedule left to record it on.
    assert execute.dispatched == ["say hi"]
    assert world.audited(schedule_id)
    assert _errors(caplog) == []
    assert len(_infos_naming(caplog, schedule_id)) == 1
    assert await world.scalar("SELECT count(*) FROM schedule") == 0
    assert await world.scalar("SELECT count(*) FROM schedule_run") == 0


async def test_delete_waits_for_the_run_record_and_cascades_it(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    world = _World(engine)
    server_id = await world.seed_server()
    schedule_id = await world.seed_schedule(server_id)
    pause = _Pause()
    execute = _Execute(world.community_id)
    tick = asyncio.create_task(
        world.runner(execute, _UnitOfWork(world.factory, pause_run_add=pause)).tick()
    )
    await pause.reached.wait()
    delete_uow = _UnitOfWork(world.factory)
    deletion = asyncio.create_task(world.delete(server_id, schedule_id, delete_uow))

    # The run record holds the schedule row: the delete waits for it.
    await await_settled(engine, deletion, lambda: delete_uow.pid)
    assert not deletion.done()
    pause.resume.set()
    await tick
    await deletion

    assert execute.dispatched == ["say hi"]
    assert world.audited(schedule_id)
    assert _errors(caplog) == []
    assert await world.scalar("SELECT count(*) FROM schedule") == 0
    assert await world.scalar("SELECT count(*) FROM schedule_run") == 0


async def _armed_retry(
    world: _World, execute: _Execute
) -> tuple[RunScheduleTick, ServerId, ScheduleId]:
    """A backup schedule whose occurrence failed, its one retry now due."""

    server_id = await world.seed_server()
    schedule_id = await world.seed_schedule(server_id, action=ScheduleAction.BACKUP)
    runner = world.runner(execute)
    execute.outcome = ScheduleRunOutcome.FAILURE
    await runner.tick()
    execute.outcome = ScheduleRunOutcome.SUCCESS
    execute.dispatched.clear()
    world.audit.events.clear()
    # Only the retry is due at the next tick, not another occurrence.
    async with world.engine.begin() as conn:
        await conn.execute(
            text("UPDATE schedule SET next_run_at = :later WHERE id = :id"),
            {"later": _NOW + dt.timedelta(days=1), "id": schedule_id.value},
        )
    world.clock.set(_NOW + dt.timedelta(minutes=2))
    return runner, server_id, schedule_id


async def test_retry_of_a_deleted_schedule_is_not_dispatched(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    world = _World(engine)
    execute = _Execute(world.community_id)
    runner, server_id, schedule_id = await _armed_retry(world, execute)
    await world.delete(server_id, schedule_id, SqlAlchemyUnitOfWork(world.factory))

    await runner.tick()

    assert execute.dispatched == []
    assert _errors(caplog) == []
    assert len(_infos_naming(caplog, schedule_id)) == 1


async def test_delete_during_a_retry_audits_the_retry(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    world = _World(engine)
    execute = _Execute(world.community_id)
    runner, server_id, schedule_id = await _armed_retry(world, execute)

    async def delete_now() -> None:
        await world.delete(server_id, schedule_id, SqlAlchemyUnitOfWork(world.factory))

    execute.on_dispatch = delete_now

    await runner.tick()

    assert execute.dispatched == [None]
    assert world.audited(schedule_id)
    assert _errors(caplog) == []
    assert len(_infos_naming(caplog, schedule_id)) == 1
    assert await world.scalar("SELECT count(*) FROM schedule_run") == 0


async def test_one_schedules_failure_keeps_the_others_work(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    world = _World(engine)
    shared = await world.seed_server()
    other = await world.seed_server()
    # The failing schedule runs first on its server; a sibling follows it on the
    # same server, and a third runs on another server.
    failing = await world.seed_schedule(
        shared, command="poison", due=_NOW - dt.timedelta(seconds=20)
    )
    sibling = await world.seed_schedule(shared, due=_NOW - dt.timedelta(seconds=10))
    elsewhere = await world.seed_schedule(other)

    await world.runner(_Execute(world.community_id)).tick()

    runs = "SELECT count(*) FROM schedule_run WHERE schedule_id = :id"
    advanced = "SELECT next_run_at > :now FROM schedule WHERE id = :id"
    assert await world.scalar(runs, id=failing.value) == 0
    assert await world.scalar(advanced, now=_NOW, id=failing.value) is False
    for kept in (sibling, elsewhere):
        assert await world.scalar(runs, id=kept.value) == 1
        assert await world.scalar(advanced, now=_NOW, id=kept.value) is True
    assert [str(failing.value) in r.getMessage() for r in _errors(caplog)] == [True]
