"""Unit tests for the game-session use cases (issue #957).

``ListGameSessions`` is community-scoped (a session whose server is outside the
path community is never returned — no cross-community signal); ``PruneGameSessions``
computes ``cutoff = now - retention`` and deletes older rows. Both run against
the in-memory fakes (NFR-TEST-1, no database).
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.servers.application.game_sessions import (
    ListGameSessions,
    PruneGameSessions,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import ServerNotFoundError
from mc_server_dashboard_api.servers.domain.game_session import GameSession
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.servers.fakes import FakeClock, FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 12, 12, 0, tzinfo=dt.timezone.utc)


def make_server(*, community_id: uuid.UUID) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=CommunityId(community_id),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.CONTAINER,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _session(server_id: ServerId, *, started_at: dt.datetime) -> GameSession:
    return GameSession(
        id=uuid.uuid4(),
        server_id=server_id,
        hostname="amber-falcon-42",
        player_ip="203.0.113.7",
        username="steve",
        player_uuid=None,
        started_at=started_at,
        ended_at=None,
    )


async def test_list_returns_sessions_for_in_community_server() -> None:
    community_id = CommunityId(uuid.uuid4())
    server = make_server(community_id=community_id.value)
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    uow.game_sessions.seed(_session(server.id, started_at=_NOW))
    result = await ListGameSessions(uow=uow)(
        community_id=community_id, server_id=server.id, limit=50, offset=0
    )
    assert len(result) == 1


async def test_list_rejects_cross_community_server() -> None:
    server = make_server(community_id=uuid.uuid4())
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    with pytest.raises(ServerNotFoundError):
        await ListGameSessions(uow=uow)(
            community_id=CommunityId(uuid.uuid4()),  # a different community
            server_id=server.id,
            limit=50,
            offset=0,
        )


async def test_list_missing_server_raises() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(ServerNotFoundError):
        await ListGameSessions(uow=uow)(
            community_id=CommunityId(uuid.uuid4()),
            server_id=ServerId(uuid.uuid4()),
            limit=50,
            offset=0,
        )


async def test_prune_uses_now_minus_retention_as_cutoff() -> None:
    uow = FakeUnitOfWork()
    server_id = ServerId(uuid.uuid4())
    uow.game_sessions.seed(
        _session(server_id, started_at=_NOW - dt.timedelta(days=100))
    )
    uow.game_sessions.seed(_session(server_id, started_at=_NOW - dt.timedelta(days=10)))
    pruner = PruneGameSessions(
        uow=uow, clock=FakeClock(_NOW), retention=dt.timedelta(days=90)
    )
    deleted = await pruner.tick()
    assert deleted == 1
    assert uow.game_sessions.deleted_before == [_NOW - dt.timedelta(days=90)]
    assert uow.commits == 1


async def test_prune_deletes_stale_end_only_placeholder() -> None:
    """End-only placeholders (started_at IS NULL) are pruned by ended_at."""
    uow = FakeUnitOfWork()
    cutoff = _NOW - dt.timedelta(days=90)
    # Stale end-only placeholder: no started_at, ended_at older than cutoff.
    stale = GameSession(
        id=uuid.uuid4(),
        server_id=None,
        hostname=None,
        player_ip=None,
        username=None,
        player_uuid=None,
        started_at=None,
        ended_at=_NOW - dt.timedelta(days=100),
    )
    # Fresh end-only placeholder: no started_at, ended_at newer than cutoff.
    fresh = GameSession(
        id=uuid.uuid4(),
        server_id=None,
        hostname=None,
        player_ip=None,
        username=None,
        player_uuid=None,
        started_at=None,
        ended_at=_NOW - dt.timedelta(days=10),
    )
    uow.game_sessions.seed(stale)
    uow.game_sessions.seed(fresh)
    deleted = await uow.game_sessions.delete_started_before(cutoff)
    assert deleted == 1
    assert len(uow.game_sessions.rows) == 1
    assert uow.game_sessions.rows[0].id == fresh.id
