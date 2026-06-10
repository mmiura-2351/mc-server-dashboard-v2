"""Unit tests for the scrape-time gauge refresh (issue #282)."""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.core.adapters import metrics
from mc_server_dashboard_api.core.adapters.metrics_refresh import refresh_scrape_gauges
from mc_server_dashboard_api.fleet.domain.entities import WorkerStatus
from mc_server_dashboard_api.fleet.domain.registry import WorkerRegistry, WorkerSnapshot
from mc_server_dashboard_api.fleet.domain.value_objects import (
    DriverKind,
    WorkerCapabilities,
    WorkerId,
)
from tests.test_metrics import _EmptyRegistry


class _CountRows:
    def __init__(self, rows: list[tuple[str, int]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, int]]:
        return self._rows


class _CountSession:
    def __init__(self, rows: list[tuple[str, int]]) -> None:
        self._rows = rows

    async def __aenter__(self) -> "_CountSession":
        return self

    async def __aexit__(self, *exc) -> None:  # type: ignore[no-untyped-def]
        return None

    async def execute(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return _CountRows(self._rows)


class _Registry(WorkerRegistry):
    def __init__(self, snapshots: list[WorkerSnapshot]) -> None:
        self._snapshots = snapshots

    def register(self, worker, held_servers=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def held_generation(self, worker_id, server_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def record_heartbeat(self, worker_id, at):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def mark_disconnected(self, worker_id, session):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def is_current_session(self, worker_id, session):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def set_draining(self, worker_id, draining):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def reserve(self, worker_id, server_id, memory_mb):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def reserved_memory_mb(self, worker_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def committed_memory_mb(self, worker_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def assignment_epoch(self, worker_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def release_reservation(self, worker_id, server_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def increment_assignment(self, worker_id, server_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def decrement_assignment(self, worker_id, server_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def set_assignment(self, worker_id, assignments, snapshot_epoch):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def candidates_for_placement(self):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def list_workers(self) -> list[WorkerSnapshot]:
        return self._snapshots

    def get(self, worker_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def _snapshot(status: WorkerStatus) -> WorkerSnapshot:
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    return WorkerSnapshot(
        id=WorkerId("worker-1"),
        version="1.0",
        capabilities=WorkerCapabilities(
            drivers=frozenset({DriverKind.HOST_PROCESS}), max_servers=1
        ),
        registered_at=now,
        last_heartbeat_at=now,
        status=status,
        assigned_count=0,
    )


@pytest.mark.asyncio
async def test_refresh_sets_servers_and_workers_gauges() -> None:
    rows = [("running", 3), ("stopped", 2)]
    registry = _Registry(
        [_snapshot(WorkerStatus.ONLINE), _snapshot(WorkerStatus.DRAINING)]
    )

    await refresh_scrape_gauges(lambda: _CountSession(rows), registry)  # type: ignore[arg-type]

    assert metrics.servers.labels(observed_state="running")._value.get() == 3.0
    assert metrics.servers.labels(observed_state="stopped")._value.get() == 2.0
    # A state with no servers is still reported as 0, not absent.
    assert metrics.servers.labels(observed_state="crashed")._value.get() == 0.0
    assert metrics.workers.labels(state="online")._value.get() == 1.0
    assert metrics.workers.labels(state="draining")._value.get() == 1.0
    assert metrics.workers.labels(state="offline")._value.get() == 0.0


@pytest.mark.asyncio
async def test_refresh_swallows_db_failure_and_counts_it() -> None:
    class _Boom:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
            return None

        async def execute(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("db down")

    before = metrics.servers_by_state_scrape_failures_total._value.get()
    # Must not raise even though the query fails.
    await refresh_scrape_gauges(lambda: _Boom(), _EmptyRegistry())  # type: ignore[arg-type]
    after = metrics.servers_by_state_scrape_failures_total._value.get()
    assert after == before + 1
