"""The ListAuditLog use case passes the filter through to the query Port."""

from __future__ import annotations

import datetime as dt
import uuid

from mc_server_dashboard_api.audit.application.list_audit_log import ListAuditLog
from mc_server_dashboard_api.audit.domain.events import AuditRecord, Outcome
from mc_server_dashboard_api.audit.domain.query import AuditFilter
from tests.audit.fakes import CapturingAuditQuery

_RECORD = AuditRecord(
    id=uuid.uuid4(),
    operation="server:create",
    outcome=Outcome.SUCCESS,
    created_at=dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc),
)


async def test_returns_records_for_filter() -> None:
    query = CapturingAuditQuery(records=[_RECORD])
    use_case = ListAuditLog(query=query)
    filter = AuditFilter(operation="server:create", limit=10)

    result = await use_case(filter)

    assert result == [_RECORD]
    assert query.last_filter == filter
