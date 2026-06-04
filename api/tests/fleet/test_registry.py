"""Unit tests for the in-memory WorkerRegistry and its liveness model.

Drives the registry against a faked Clock (no wall-clock dependence,
TESTING.md Section 4): registration, heartbeat refresh, expiry past the
timeout, and disconnect marking offline.
"""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.domain.entities import WorkerStatus
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from tests.fleet.fakes import FakeClock, make_worker

_T0 = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_TIMEOUT = dt.timedelta(seconds=30)


def _registry(clock: FakeClock) -> InMemoryWorkerRegistry:
    return InMemoryWorkerRegistry(clock=clock, heartbeat_timeout=_TIMEOUT)


def test_registered_worker_is_listed_online() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))

    workers = registry.list_workers()

    assert len(workers) == 1
    assert workers[0].id == WorkerId("worker-1")
    assert workers[0].status is WorkerStatus.ONLINE
    assert workers[0].version == "1.0.0"


def test_worker_goes_offline_after_heartbeat_timeout() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))

    clock.set(_T0 + _TIMEOUT + dt.timedelta(seconds=1))

    assert registry.list_workers()[0].status is WorkerStatus.OFFLINE


def test_worker_at_exactly_timeout_is_still_online() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))

    clock.set(_T0 + _TIMEOUT)

    assert registry.list_workers()[0].status is WorkerStatus.ONLINE


def test_heartbeat_refreshes_liveness() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))

    later = _T0 + dt.timedelta(seconds=25)
    registry.record_heartbeat(WorkerId("worker-1"), later)
    # Now within the window measured from the fresh heartbeat, not registration.
    clock.set(later + dt.timedelta(seconds=20))

    snapshot = registry.list_workers()[0]
    assert snapshot.status is WorkerStatus.ONLINE
    assert snapshot.last_heartbeat_at == later


def test_disconnect_marks_offline_within_window() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))

    registry.mark_disconnected(WorkerId("worker-1"))

    # Still well inside the heartbeat window, but disconnect forces offline.
    assert registry.list_workers()[0].status is WorkerStatus.OFFLINE


def test_reregistration_replaces_prior_record() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))
    registry.mark_disconnected(WorkerId("worker-1"))

    # A fresh Session re-registers from scratch (CONTROL_PLANE.md Section 4.4).
    registry.register(make_worker(at=_T0, version="2.0.0"))

    workers = registry.list_workers()
    assert len(workers) == 1
    assert workers[0].status is WorkerStatus.ONLINE
    assert workers[0].version == "2.0.0"


def test_heartbeat_for_unknown_worker_is_ignored() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)

    registry.record_heartbeat(WorkerId("ghost"), _T0)

    assert registry.list_workers() == []
