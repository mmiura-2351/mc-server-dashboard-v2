"""Unit tests for the parse-failure paths of :class:`ServersLateSnapshotResultSink`.

A worker id is enforced to be a UUID at registration (issue #99), and server ids
are DB-issued UUIDs. A non-UUID reaching the sink is an invariant violation at the
control-plane seam; the sink must surface it loudly (an error log) instead of
silently clearing nothing. These tests never touch a database: the parse check
runs before the session factory opens, so the factory raises if ever called.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import NoReturn, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.servers.adapters.late_snapshot_result_sink import (
    ServersLateSnapshotResultSink,
)
from tests.servers.fakes import FakeClock, FakeControlPlane

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_UUID = "22222222-2222-2222-2222-222222222222"


def _exploding_factory() -> NoReturn:  # pragma: no cover - asserts it never opens
    raise AssertionError("session factory must not open on a parse failure")


def _sink() -> ServersLateSnapshotResultSink:
    factory = cast(async_sessionmaker[AsyncSession], _exploding_factory)
    return ServersLateSnapshotResultSink(
        factory, control_plane=FakeControlPlane(), clock=FakeClock(_NOW)
    )


async def test_clear_logs_on_non_uuid_server_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        await _sink().clear_held_assignment_on_late_snapshot(
            server_id="server-1", worker_id=_UUID, succeeded=False
        )
    assert any(record.levelno == logging.ERROR for record in caplog.records)


async def test_clear_logs_on_non_uuid_worker_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        await _sink().clear_held_assignment_on_late_snapshot(
            server_id=_UUID, worker_id="worker-1", succeeded=True
        )
    assert any(record.levelno == logging.ERROR for record in caplog.records)
