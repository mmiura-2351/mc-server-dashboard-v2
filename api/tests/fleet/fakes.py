"""In-memory test doubles for the fleet context (TESTING.md Section 4)."""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.fleet.domain.clock import Clock
from mc_server_dashboard_api.fleet.domain.entities import Worker
from mc_server_dashboard_api.fleet.domain.real_time_events import (
    EventStream,
    EventSubscription,
    RealTimeEvent,
    RealTimeEvents,
)
from mc_server_dashboard_api.fleet.domain.server_state_sink import ServerStateSink
from mc_server_dashboard_api.fleet.domain.value_objects import (
    DriverKind,
    HostResources,
    WorkerCapabilities,
    WorkerId,
)


class FakeClock(Clock):
    def __init__(self, now: dt.datetime) -> None:
        self._now = now

    def set(self, now: dt.datetime) -> None:
        self._now = now

    def now(self) -> dt.datetime:
        return self._now


class FakeServerStateSink(ServerStateSink):
    """Records control-plane reconciliation calls; configurable running tally.

    The servicer drives this on a StatusChange (observed-state cache), on
    disconnect (mark unknown), and on register (rebuild assignment count).
    """

    def __init__(
        self,
        *,
        running_ids: dict[str, set[str]] | None = None,
        fail_observed_for: set[str] | None = None,
        always_fail_observed: bool = False,
        known_server_ids: set[str] | None = None,
        reject_observed_for: set[str] | None = None,
    ) -> None:
        self.observed: list[tuple[str, str, str]] = []
        self.rejected: list[tuple[str, str, str]] = []
        self.unknown_for: list[str] = []
        self.counted_for: list[str] = []
        self._running_ids = running_ids or {}
        # Server ids whose next record_observed_state call raises, simulating a
        # transient DB error while handling one StatusChange; the id is dropped
        # after raising so a later report for the same server succeeds.
        self._fail_observed_for = fail_observed_for or set()
        # When set, every record_observed_state call raises, simulating a sink
        # that is permanently down (e.g. the DB is unreachable); used to exercise
        # the consecutive-failure containment cap (issue #807).
        self._always_fail_observed = always_fail_observed
        # The set of server ids the sink reports as existing (issue #924). When
        # None, all ids are treated as existing (the default for tests that do not
        # exercise the unknown-held-server path).
        self._known_server_ids = known_server_ids
        # Server ids whose record_observed_state returns False (applied=False),
        # simulating a monotonic/ownership guard rejection (issue #1957).
        self._reject_observed_for = reject_observed_for or set()

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str
    ) -> bool:
        if self._always_fail_observed:
            raise RuntimeError("observed-state sink unavailable")
        if server_id in self._fail_observed_for:
            self._fail_observed_for.discard(server_id)
            raise RuntimeError("transient observed-state write failure")
        if server_id in self._reject_observed_for:
            self.rejected.append((server_id, worker_id, state))
            return False
        self.observed.append((server_id, worker_id, state))
        return True

    async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
        self.unknown_for.append(worker_id)

    async def existing_server_ids(self, *, server_ids: list[str]) -> set[str]:
        if self._known_server_ids is None:
            return set(server_ids)
        # Match the real adapter's contract: unparseable (non-UUID) IDs are
        # treated as existing so they are never misclassified as deleted (issue
        # #924 review). Only IDs that parse as UUID and are NOT in the known set
        # are reported absent.
        import uuid

        result: set[str] = set()
        for sid in server_ids:
            try:
                uuid.UUID(sid)
            except ValueError:
                result.add(sid)  # unparseable -> safe (existing)
                continue
            if sid in self._known_server_ids:
                result.add(sid)
        return result

    async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
        self.counted_for.append(worker_id)
        # Configured by id only; declared memory is unset (0) for these fakes —
        # the rebuild's memory-restore path is exercised by the registry unit tests.
        return {server_id: 0 for server_id in self._running_ids.get(worker_id, set())}


class RecordingRealTimeEvents(RealTimeEvents):
    """Records every published event by server id; subscribe is unused here.

    The servicer-publish tests assert events flow from a fake session into this
    sink without the session path awaiting any subscriber consumption.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, RealTimeEvent]] = []

    def publish(self, *, server_id: str, event: RealTimeEvent) -> None:
        self.published.append((server_id, event))

    def subscribe(
        self, *, server_id: str, streams: frozenset[EventStream]
    ) -> EventSubscription:  # pragma: no cover - unused in these tests
        raise NotImplementedError

    def subscribe_all(
        self, *, streams: frozenset[EventStream]
    ) -> EventSubscription:  # pragma: no cover - unused in these tests
        raise NotImplementedError


def make_worker(
    *,
    worker_id: str = "worker-1",
    version: str = "1.0.0",
    at: dt.datetime,
    drivers: frozenset[DriverKind] = frozenset({DriverKind.CONTAINER}),
    max_servers: int = 4,
    resources: HostResources = HostResources(cpu_cores=8, memory_bytes=16_000_000_000),
) -> Worker:
    return Worker(
        id=WorkerId(worker_id),
        version=version,
        capabilities=WorkerCapabilities(
            drivers=drivers,
            max_servers=max_servers,
            resources=resources,
        ),
        registered_at=at,
        last_heartbeat_at=at,
    )
