"""In-memory fakes for the audit Ports used by the audit tests.

Keeps the recorder, query use case, and route tests against fakes (no database),
per TESTING.md Section 4.
"""

from __future__ import annotations

import datetime as dt
import uuid

from mc_server_dashboard_api.audit.domain.clock import Clock
from mc_server_dashboard_api.audit.domain.events import AuditEvent, AuditRecord
from mc_server_dashboard_api.audit.domain.name_resolver import NameResolver
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


class FakeNameResolver(NameResolver):
    """Resolves ids from in-memory maps; ids absent from a map are 'deleted'.

    Records the id lists it was asked for so a test can assert the lookup is
    batched (distinct ids, one call per kind) rather than per-row N+1.
    """

    def __init__(
        self,
        *,
        usernames: dict[uuid.UUID, str] | None = None,
        server_names: dict[uuid.UUID, str] | None = None,
        community_names: dict[uuid.UUID, str] | None = None,
    ) -> None:
        self._usernames = usernames or {}
        self._server_names = server_names or {}
        self._community_names = community_names or {}
        self.user_id_calls: list[list[uuid.UUID]] = []
        self.server_id_calls: list[list[uuid.UUID]] = []
        self.community_id_calls: list[list[uuid.UUID]] = []

    async def resolve_usernames(
        self, user_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        self.user_id_calls.append(list(user_ids))
        return {uid: self._usernames[uid] for uid in user_ids if uid in self._usernames}

    async def resolve_server_names(
        self, server_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        self.server_id_calls.append(list(server_ids))
        return {
            sid: self._server_names[sid]
            for sid in server_ids
            if sid in self._server_names
        }

    async def resolve_community_names(
        self, community_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        self.community_id_calls.append(list(community_ids))
        return {
            cid: self._community_names[cid]
            for cid in community_ids
            if cid in self._community_names
        }
