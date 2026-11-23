"""Unit tests for the parse-failure paths of :class:`ServersServerStateSink`.

The last test pins the crash-reason vocabulary the sink translates (#3281).

A worker id is enforced to be a UUID at registration (issue #99), and server
ids are DB-issued UUIDs. A non-UUID reaching the sink is therefore an invariant
violation; the sink must surface it loudly (an error log) instead of silently
no-opping. These tests never touch a database: the parse check runs before the
session factory is opened, so the factory raises if it is ever called.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import NoReturn, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.fleet.adapters.grpc_server import _CRASH_REASON_BY_PROTO
from mc_server_dashboard_api.servers.adapters.models import _CRASH_REASONS
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mc_server_dashboard_api.servers.domain.value_objects import CrashReason
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from tests.servers.fakes import FakeClock

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_UUID = "22222222-2222-2222-2222-222222222222"


def _exploding_factory() -> NoReturn:  # pragma: no cover - asserts it never opens
    raise AssertionError("session factory must not open on a parse failure")


def _sink() -> ServersServerStateSink:
    factory = cast(async_sessionmaker[AsyncSession], _exploding_factory)
    return ServersServerStateSink(factory, clock=FakeClock(_NOW))


async def test_record_observed_state_logs_on_non_uuid_worker_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        await _sink().record_observed_state(
            server_id=_UUID, worker_id="worker-1", state="running", observed_at=_NOW
        )
    assert any(record.levelno == logging.ERROR for record in caplog.records)


async def test_mark_worker_servers_unknown_logs_on_non_uuid_worker_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        await _sink().mark_worker_servers_unknown(worker_id="worker-1")
    assert any(record.levelno == logging.ERROR for record in caplog.records)


async def test_running_assignment_ids_logs_on_non_uuid_worker_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        ids = await _sink().running_assignment_ids(worker_id="worker-1")
    assert ids == {}
    assert any(record.levelno == logging.ERROR for record in caplog.records)


def test_every_relayed_crash_reason_is_one_the_row_stores() -> None:
    # Issue #3281: the sink stores the reason the servicer relays. A name in
    # one vocabulary and not the other would fail the write, and the crashed
    # state with it, so the wire enum, the relayed names, the domain enum and
    # the column's CHECK must name the same reasons.
    relayed = set(_CRASH_REASON_BY_PROTO.values())

    assert set(_CRASH_REASON_BY_PROTO) == {
        value
        for value in pb.CrashReason.values()
        if value != pb.CRASH_REASON_UNSPECIFIED
    }
    assert relayed == {reason.value for reason in CrashReason}
    assert relayed == set(_CRASH_REASONS)
