"""Use-case tests for the legacy NULL-game_port startup WARN (issue #310).

The check is read-only and informational: it returns the count of servers with
no tracked game port and WARN-logs their ids so an operator can backfill them. A
deployment with no legacy rows logs nothing. Run against in-memory fakes
(TESTING.md Section 4).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

import pytest

from mc_server_dashboard_api.servers.application.warn_missing_ports import (
    WarnLegacyMissingPorts,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.servers.fakes import FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


def _server(*, community_id: CommunityId, game_port: int | None) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=community_id,
        name=ServerName(f"srv-{uuid.uuid4().hex[:8]}"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.HOST_PROCESS,
        config={},
        game_port=game_port,
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


async def test_returns_zero_and_logs_nothing_without_legacy_rows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    uow.servers.seed(_server(community_id=community, game_port=25565))
    with caplog.at_level(logging.WARNING):
        count = await WarnLegacyMissingPorts(uow=uow)()
    assert count == 0
    assert caplog.records == []


async def test_counts_and_warns_legacy_rows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    uow = FakeUnitOfWork()
    community = CommunityId(uuid.uuid4())
    legacy_a = _server(community_id=community, game_port=None)
    legacy_b = _server(community_id=community, game_port=None)
    uow.servers.seed(legacy_a)
    uow.servers.seed(legacy_b)
    uow.servers.seed(_server(community_id=community, game_port=25565))
    with caplog.at_level(logging.WARNING):
        count = await WarnLegacyMissingPorts(uow=uow)()
    assert count == 2
    record = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert record.__dict__["count"] == 2
    assert set(record.__dict__["server_ids"]) == {
        str(legacy_a.id.value),
        str(legacy_b.id.value),
    }
