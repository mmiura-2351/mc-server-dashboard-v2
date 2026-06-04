"""The SQLAlchemy writer stamps id + event time and uses its own transaction.

Unit-level: the session factory is faked to a recording stand-in so the test
stays off the database (NFR-TEST-1). The behaviour under test is that the writer
maps the event onto a model row, stamps the clock's time, and commits once.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from mc_server_dashboard_api.audit.adapters.writer import SqlAlchemyAuditWriter
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from tests.audit.fakes import FakeClock

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commits = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _factory(session: _FakeSession):  # type: ignore[no-untyped-def]
    def make() -> _FakeSession:
        return session

    return make


async def test_write_maps_event_and_commits_once() -> None:
    session = _FakeSession()
    writer = SqlAlchemyAuditWriter(_factory(session), clock=FakeClock(_NOW))
    actor = uuid.uuid4()
    community = uuid.uuid4()
    target = uuid.uuid4()

    await writer.write(
        AuditEvent(
            operation="server:delete",
            outcome=Outcome.SUCCESS,
            actor_id=actor,
            community_id=community,
            target_type="server",
            target_id=target,
        )
    )

    assert session.commits == 1
    assert len(session.added) == 1
    row = session.added[0]
    assert row.actor_id == actor
    assert row.community_id == community
    assert row.operation == "server:delete"
    assert row.target_type == "server"
    assert row.target_id == target
    assert row.outcome == "success"
    assert row.created_at == _NOW
    assert isinstance(row.id, uuid.UUID)
