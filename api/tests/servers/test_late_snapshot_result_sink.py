"""Unit tests for :class:`ServersLateSnapshotResultSink`, the control-plane seam.

A worker id is enforced to be a UUID at registration (issue #99), and server ids
are DB-issued UUIDs. A non-UUID reaching the sink is an invariant violation at the
control-plane seam; the sink must surface it loudly (an error log) instead of
silently clearing nothing. The parse check runs before the session factory opens,
so those tests never touch a database: the factory raises if ever called.

A well-formed call must carry the whole result across to the use case, the
Worker's failure detail included (issue #2766) — this delegation is the seam that
would otherwise drop it, leaving the release log with no cause to name. That test
substitutes the UnitOfWork the sink builds per call, so the real use case and its
release log run over in-memory fakes rather than a database.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import NoReturn, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.servers.adapters import (
    late_snapshot_result_sink as sink_module,
)
from mc_server_dashboard_api.servers.adapters.late_snapshot_result_sink import (
    ServersLateSnapshotResultSink,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from tests.servers.fakes import FakeClock, FakeControlPlane, FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_UUID = "22222222-2222-2222-2222-222222222222"


def _exploding_factory() -> NoReturn:  # pragma: no cover - asserts it never opens
    raise AssertionError("session factory must not open on a parse failure")


def _sink() -> ServersLateSnapshotResultSink:
    factory = cast(async_sessionmaker[AsyncSession], _exploding_factory)
    return ServersLateSnapshotResultSink(
        factory, control_plane=FakeControlPlane(), clock=FakeClock(_NOW)
    )


def _held_server(server_id: uuid.UUID, worker_id: uuid.UUID) -> Server:
    """A row held at (stopped, stopped, assigned) — what the late clear releases."""

    return Server(
        id=ServerId(server_id),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=None,
        assigned_worker_id=WorkerId(worker_id),
        created_at=_NOW,
        updated_at=_NOW,
    )


async def test_clear_logs_on_non_uuid_server_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        await _sink().clear_held_assignment_on_late_snapshot(
            server_id="server-1",
            worker_id=_UUID,
            succeeded=False,
            message="transfer_failed",
        )
    assert any(record.levelno == logging.ERROR for record in caplog.records)


async def test_clear_logs_on_non_uuid_worker_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        await _sink().clear_held_assignment_on_late_snapshot(
            server_id=_UUID,
            worker_id="worker-1",
            succeeded=True,
            message=None,
        )
    assert any(record.levelno == logging.ERROR for record in caplog.records)


async def test_clear_carries_the_worker_message_into_the_release_warning(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue #2766: the Worker's failure detail is the only text that names WHY the
    # late snapshot failed — here the compose-internal data-plane URL a second-host
    # worker was handed (#2595/#2765). This delegation is the seam that dropped it,
    # so drive the real adapter with well-formed ids and assert the detail comes out
    # of the real use case's release WARN, verbatim. Only the per-call UnitOfWork is
    # substituted (in-memory fakes, no database); everything from the parse through
    # the log is production code.
    server_id, worker_id = uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    uow.servers.seed(_held_server(server_id, worker_id))
    monkeypatch.setattr(sink_module, "SqlAlchemyUnitOfWork", lambda _factory: uow)
    detail = (
        "instancemanager: snapshot: datatransfer: snapshot request: Post "
        '"http://api:8000/api/data-plane/communities/c/servers/s/working-set": '
        "dial tcp: lookup api on 127.0.0.11:53: no such host"
    )

    with caplog.at_level(logging.WARNING):
        await _sink().clear_held_assignment_on_late_snapshot(
            server_id=str(server_id),
            worker_id=str(worker_id),
            succeeded=False,
            message=detail,
        )

    assert any(
        record.levelno == logging.WARNING and detail in record.getMessage()
        for record in caplog.records
    )
