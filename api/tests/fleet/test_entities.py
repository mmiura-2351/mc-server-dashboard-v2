"""Unit tests for the Worker entity's pure liveness math (FR-WRK-2)."""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.fleet.domain.entities import WorkerStatus
from mc_server_dashboard_api.fleet.domain.errors import InvalidWorkerIdError
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from tests.fleet.fakes import make_worker

_T0 = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_TIMEOUT = dt.timedelta(seconds=30)


def test_fresh_worker_is_online() -> None:
    worker = make_worker(at=_T0)
    assert worker.status(now=_T0, timeout=_TIMEOUT) is WorkerStatus.ONLINE


def test_status_offline_past_timeout() -> None:
    worker = make_worker(at=_T0)
    now = _T0 + _TIMEOUT + dt.timedelta(milliseconds=1)
    assert worker.status(now=now, timeout=_TIMEOUT) is WorkerStatus.OFFLINE


def test_with_heartbeat_moves_the_window() -> None:
    worker = make_worker(at=_T0)
    refreshed = worker.with_heartbeat(_T0 + dt.timedelta(seconds=40))
    assert (
        refreshed.status(now=_T0 + dt.timedelta(seconds=50), timeout=_TIMEOUT)
        is WorkerStatus.ONLINE
    )


def test_disconnected_worker_is_offline_even_when_fresh() -> None:
    worker = make_worker(at=_T0).disconnect()
    assert worker.status(now=_T0, timeout=_TIMEOUT) is WorkerStatus.OFFLINE


def test_draining_worker_reports_draining_while_live() -> None:
    worker = make_worker(at=_T0).start_draining()
    assert worker.status(now=_T0, timeout=_TIMEOUT) is WorkerStatus.DRAINING


def test_draining_offline_worker_is_offline() -> None:
    # Liveness wins over drain: a draining Worker that has gone offline reports
    # OFFLINE, not DRAINING.
    worker = make_worker(at=_T0).start_draining().disconnect()
    assert worker.status(now=_T0, timeout=_TIMEOUT) is WorkerStatus.OFFLINE


def test_stop_draining_returns_to_online() -> None:
    worker = make_worker(at=_T0).start_draining().stop_draining()
    assert worker.status(now=_T0, timeout=_TIMEOUT) is WorkerStatus.ONLINE


def test_blank_worker_id_is_rejected() -> None:
    with pytest.raises(InvalidWorkerIdError):
        WorkerId("   ")
