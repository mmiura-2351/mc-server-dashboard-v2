"""In-memory fakes for the audit Ports used by the audit tests.

Keeps the recorder, query use case, and route tests against fakes (no database),
per TESTING.md Section 4.
"""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.audit.domain.clock import Clock
from mc_server_dashboard_api.audit.domain.events import AuditEvent, AuditRecord
from mc_server_dashboard_api.audit.domain.query import AuditFilter, AuditQuery
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.audit.domain.writer import AuditWriter


class FakeClock(Clock):
    def __init__(self, now: dt.datetime) -> None:
        self._now = now

    def now(self) -> dt.datetime:
        return self._now


class RecordingAuditWriter(AuditWriter):
    """Captures written events; optionally raises to exercise must-not-raise."""

    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[AuditEvent] = []
        self._fail = fail

    async def write(self, event: AuditEvent) -> None:
        if self._fail:
            raise RuntimeError("audit backend down")
        self.events.append(event)


class RecordingAuditRecorder(AuditRecorder):
    """Captures recorded events for route-coverage assertions."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


class CapturingAuditQuery(AuditQuery):
    """Returns a fixed list and remembers the filter it was called with."""

    def __init__(self, records: list[AuditRecord] | None = None) -> None:
        self.records = records or []
        self.last_filter: AuditFilter | None = None

    async def list_records(self, filter: AuditFilter) -> list[AuditRecord]:
        self.last_filter = filter
        return self.records
