"""A run record whose COMMIT applied but was answered as failed (issue #4141).

The runner keeps a run whose record raised owed, and records it again on a
later tick (issue #4108). A COMMIT whose answer is lost with its connection
raises although it applied (the #4062 shape), so that later record finds the
run already written. The run keeps its id across the attempts and the insert is
idempotent on it: no second row is written, and the run is audited once. A
backup retry held past its deferral's bound is recorded as skipped and is not
owed: the next tick decides the same skip again, and records it under the id it
kept (issue #4166).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (a real PostgreSQL); skipped
otherwise (TESTING.md Section 5). The runner writes through a proxy that drops
the answer to the first run record's COMMIT, and closes that connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import os
from collections.abc import AsyncIterator
from typing import cast

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine

from mc_server_dashboard_api.audit.domain.operations import SCHEDULE_RUN
from mc_server_dashboard_api.core.adapters.database import (
    create_engine,
    create_session_factory,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.servers.application.schedule_runner import (
    Deferred,
    ExecuteScheduleAction,
)
from mc_server_dashboard_api.servers.domain.schedule import (
    ScheduleAction,
    ScheduleId,
    ScheduleRunOutcome,
)
from tests.integration.races import race_database
from tests.integration.test_held_report_frame import _LostAnswerProxy
from tests.integration.test_schedule_runner_delete_races import (
    _NOW,
    _Execute,
    _World,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    async with race_database(_DB_URL) as eng:
        yield eng


@contextlib.asynccontextmanager
async def _run_record_answer_lost() -> AsyncIterator[
    tuple[_LostAnswerProxy, AsyncEngine]
]:
    """An engine whose first COMMIT of a run record applies, and whose answer is
    lost with its connection: the record raises although its write landed."""

    assert _DB_URL is not None
    target = make_url(_DB_URL)
    assert target.host is not None and target.port is not None
    proxy = _LostAnswerProxy(target.host, target.port)
    listener = await asyncio.start_server(proxy.serve, "127.0.0.1", 0)
    url = target.set(host="127.0.0.1", port=listener.sockets[0].getsockname()[1])
    engine = create_engine(
        url.render_as_string(hide_password=False), statement_timeout=10.0
    )
    recording = False

    def _note_the_record(
        _conn: object, _cursor: object, statement: str, *_rest: object
    ) -> None:
        nonlocal recording
        if statement.startswith("INSERT INTO schedule_run"):
            recording = True

    def _lose_the_answer(_conn: object) -> None:
        # Fires just before the COMMIT is sent: the tick's polls commit too, so
        # only the transaction that wrote the run row loses its answer.
        nonlocal recording
        if recording and not proxy.cuts:
            proxy.armed = True
        recording = False

    sa_event.listen(engine.sync_engine, "before_cursor_execute", _note_the_record)
    sa_event.listen(engine.sync_engine, "commit", _lose_the_answer)
    try:
        yield proxy, engine
    finally:
        await engine.dispose()
        listener.close()
        for writer in proxy.writers:
            writer.close()


async def _keep_the_next_occurrence_clear(
    engine: AsyncEngine, schedule_id: ScheduleId
) -> None:
    """Move the schedule's next occurrence a day out. The first record's advance
    puts it on the id-jittered interval grid, which can land inside the ticks
    that follow."""

    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE schedule SET next_run_at = :later WHERE id = :id"),
            {"later": _NOW + dt.timedelta(days=1), "id": schedule_id.value},
        )


async def test_a_run_whose_record_answer_was_lost_is_recorded_and_audited_once(
    engine: AsyncEngine,
) -> None:
    world = _World(engine)
    server_id = await world.seed_server()
    schedule_id = await world.seed_schedule(server_id)
    execute = _Execute(world.community_id)
    async with _run_record_answer_lost() as (proxy, proxied):
        runner = world.runner(
            execute, SqlAlchemyUnitOfWork(create_session_factory(proxied))
        )
        await runner.tick()  # the record applies, and raises: owed
        await _keep_the_next_occurrence_clear(engine, schedule_id)
        world.clock.set(_NOW + dt.timedelta(seconds=20))
        await runner.tick()  # the owed record is retried

    assert proxy.cuts == 1
    assert execute.dispatched == ["say hi"]
    assert await world.scalar("SELECT count(*) FROM schedule_run") == 1
    assert [
        event.target_id
        for event in world.audit.events
        if event.operation == SCHEDULE_RUN
    ] == [schedule_id.value]
    assert runner._owed == {}


async def _held(**_kwargs: object) -> Deferred:
    """The server is at rest, and a stop still holds it for its final snapshot."""

    return Deferred()


async def test_a_held_retry_skip_whose_record_answer_was_lost_is_recorded_once(
    engine: AsyncEngine,
) -> None:
    world = _World(engine)
    server_id = await world.seed_server()
    schedule_id = await world.seed_schedule(server_id, action=ScheduleAction.BACKUP)
    runner = world.runner(
        _Execute(world.community_id, outcome=ScheduleRunOutcome.FAILURE)
    )
    await runner.tick()  # the occurrence fails, and arms its retry
    await _keep_the_next_occurrence_clear(engine, schedule_id)
    runner.make_execute = lambda: cast(ExecuteScheduleAction, _held)
    retry_at = _NOW + runner.backup_retry_delay
    world.clock.set(retry_at)
    await runner.tick()  # the retry is deferred
    past = retry_at + runner.deferral_bound + dt.timedelta(seconds=20)
    async with _run_record_answer_lost() as (proxy, proxied):
        runner.uow = SqlAlchemyUnitOfWork(create_session_factory(proxied))
        world.clock.set(past)
        await runner.tick()  # the skip's record applies, and raises
        world.clock.set(past + dt.timedelta(seconds=20))
        await runner.tick()  # still held: the same skip is recorded again

    assert proxy.cuts == 1
    assert await world.scalar("SELECT count(*) FROM schedule_run") == 2
    assert (
        await world.scalar(
            "SELECT count(*) FROM schedule_run WHERE outcome = 'skipped'"
        )
        == 1
    )
    assert runner._backup_retry == {}
