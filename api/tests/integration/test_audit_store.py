"""Integration tests for the audit writer + query adapters on PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). The 0007 migration creates/teardowns
the ``audit_log`` table, so the adapters run against the documented shape
(DATABASE.md Section 9). Verifies fire-after-commit independence (FR-AUD-2): the
writer commits in its own transaction, unaffected by an outer rolled-back
session, and -- the converse ordering invariant -- a never-recorded event leaves
no row.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.audit.adapters.models import AuditLogModel
from mc_server_dashboard_api.audit.adapters.query import SqlAlchemyAuditQuery
from mc_server_dashboard_api.audit.adapters.writer import SqlAlchemyAuditWriter
from mc_server_dashboard_api.audit.domain.clock import Clock
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.query import AuditFilter
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_T0 = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


class _FixedClock(Clock):
    def __init__(self, now: dt.datetime) -> None:
        self._now = now

    def set(self, now: dt.datetime) -> None:
        self._now = now

    def now(self) -> dt.datetime:
        return self._now


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


async def _row_count(engine: AsyncEngine) -> int:
    async with create_session_factory(engine)() as session:
        stmt = select(func.count()).select_from(AuditLogModel)
        return (await session.execute(stmt)).scalar_one()


async def test_write_persists_independently_of_outer_rollback(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    writer = SqlAlchemyAuditWriter(factory, clock=_FixedClock(_T0))

    # Simulate a business UoW that opens a session, stages nothing useful, and
    # rolls back -- the writer's own short transaction must still have committed.
    async with factory() as outer:
        await writer.write(
            AuditEvent(operation="community:provision", outcome=Outcome.SUCCESS)
        )
        await outer.rollback()

    assert await _row_count(engine) == 1


async def test_never_recorded_event_leaves_no_row(engine: AsyncEngine) -> None:
    # The converse ordering invariant: if the operation rolled back the recorder
    # is never invoked, so no audit row exists (FR-AUD-2 fire-after-commit).
    assert await _row_count(engine) == 0


async def test_query_filters_and_orders_newest_first(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    clock = _FixedClock(_T0)
    writer = SqlAlchemyAuditWriter(factory, clock=clock)
    community = uuid.uuid4()
    other = uuid.uuid4()
    actor = uuid.uuid4()

    clock.set(_T0)
    await writer.write(
        AuditEvent(
            operation="server:create",
            outcome=Outcome.SUCCESS,
            community_id=community,
            actor_id=actor,
        )
    )
    clock.set(_T0 + dt.timedelta(minutes=1))
    await writer.write(
        AuditEvent(
            operation="server:delete",
            outcome=Outcome.SUCCESS,
            community_id=community,
            actor_id=actor,
        )
    )
    clock.set(_T0 + dt.timedelta(minutes=2))
    await writer.write(
        AuditEvent(
            operation="server:create",
            outcome=Outcome.SUCCESS,
            community_id=other,
            actor_id=actor,
        )
    )

    query = SqlAlchemyAuditQuery(factory)

    # Community scoping: only this community's rows.
    scoped = await query.list_records(AuditFilter(community_id=community))
    assert {r.operation for r in scoped} == {"server:create", "server:delete"}
    # Newest first.
    assert scoped[0].operation == "server:delete"

    # Operation filter, across communities.
    creates = await query.list_records(AuditFilter(operation="server:create"))
    assert len(creates) == 2

    # Time-range filter (until is exclusive).
    early = await query.list_records(AuditFilter(until=_T0 + dt.timedelta(minutes=1)))
    assert [r.operation for r in early] == ["server:create"]

    # Pagination.
    page = await query.list_records(AuditFilter(limit=1, offset=1))
    assert len(page) == 1
