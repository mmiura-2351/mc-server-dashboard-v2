"""Integration tests for the player-group repository on PostgreSQL (issue #276).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). The schema is created and torn down per
test via the real 0001-0012 migrations so the adapter runs against the documented
shape. A community and a server are seeded; the repository's CRUD, player upsert,
attach/detach, and the cross-direction listings are exercised end to end, plus the
``ON DELETE CASCADE`` from server and group deletion.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId as CommunityCommunityId,
)
from mc_server_dashboard_api.community.domain.value_objects import CommunityName
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.domain.groups import (
    GroupId,
    GroupKind,
    GroupName,
    Player,
    PlayerGroup,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 5, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


async def _seed_community(engine: AsyncEngine) -> uuid.UUID:
    community = Community(
        id=CommunityCommunityId(uuid.uuid4()),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with CommunityUnitOfWork(create_session_factory(engine)) as uow:
        await uow.communities.add(community)
        await uow.commit()
    return community.id.value


async def _seed_server(engine: AsyncEngine, community_id: uuid.UUID) -> uuid.UUID:
    server_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO server (id, community_id, name, mc_edition, mc_version, "
                "server_type, execution_backend, config, desired_state, "
                "observed_state, created_at, updated_at) VALUES "
                "(:id, :cid, 'srv', 'java', '1.21.1', 'vanilla', 'host_process', "
                "'{}', 'stopped', 'stopped', :at, :at)"
            ),
            {"id": server_id, "cid": community_id, "at": _NOW},
        )
    return server_id


def _group(community_id: uuid.UUID, players: list[Player]) -> PlayerGroup:
    return PlayerGroup(
        id=GroupId.new(),
        community_id=CommunityId(community_id),
        name=GroupName("admins"),
        kind=GroupKind.OP,
        players=players,
    )


async def test_add_get_and_player_save(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    pid = uuid.uuid4()
    group = _group(community_id, [Player(pid, "alice")])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.groups.get_by_id(group.id)
    assert loaded is not None
    assert [(p.uuid, p.username) for p in loaded.players] == [(pid, "alice")]

    # Upsert the username and persist (delete-then-insert player set).
    loaded.upsert_player(Player(pid, "alice2"))
    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.save(loaded)
        await uow.commit()
    async with ServersUnitOfWork(factory) as uow:
        again = await uow.groups.get_by_id(group.id)
    assert again is not None
    assert again.players[0].username == "alice2"


async def test_add_group_with_players_round_trips(engine: AsyncEngine) -> None:
    # Regression: ``add`` must flush the parent player_group row before the
    # group_player children, or the child INSERT violates the FK. Persisting a
    # group that already carries players must round-trip the whole set.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    group = _group(community_id, [Player(p1, "alice"), Player(p2, "bob")])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.groups.get_by_id(group.id)
    assert loaded is not None
    assert {(p.uuid, p.username) for p in loaded.players} == {
        (p1, "alice"),
        (p2, "bob"),
    }


async def test_attach_detach_and_listings(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    server_id = await _seed_server(engine, community_id)
    factory = create_session_factory(engine)
    group = _group(community_id, [])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.groups.attach(group.id, ServerId(server_id))
        # Re-attach is idempotent.
        await uow.groups.attach(group.id, ServerId(server_id))
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        assert await uow.groups.is_attached(group.id, ServerId(server_id)) is True
        servers = await uow.groups.list_server_ids_for_group(group.id)
        groups = await uow.groups.list_groups_for_server(ServerId(server_id))
        op_groups = await uow.groups.list_groups_for_server_kind(
            ServerId(server_id), GroupKind.OP
        )
    assert [s.value for s in servers] == [server_id]
    assert [g.id for g in groups] == [group.id]
    assert [g.id for g in op_groups] == [group.id]

    async with ServersUnitOfWork(factory) as uow:
        assert await uow.groups.detach(group.id, ServerId(server_id)) is True
        await uow.commit()
    async with ServersUnitOfWork(factory) as uow:
        assert await uow.groups.is_attached(group.id, ServerId(server_id)) is False


async def test_delete_group_cascades_players_and_attachments(
    engine: AsyncEngine,
) -> None:
    community_id = await _seed_community(engine)
    server_id = await _seed_server(engine, community_id)
    factory = create_session_factory(engine)
    group = _group(community_id, [Player(uuid.uuid4(), "alice")])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.groups.attach(group.id, ServerId(server_id))
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.delete(group.id)
        await uow.commit()

    async with engine.connect() as conn:
        players = (
            await conn.execute(text("SELECT count(*) FROM group_player"))
        ).scalar_one()
        attachments = (
            await conn.execute(text("SELECT count(*) FROM server_group"))
        ).scalar_one()
    assert players == 0
    assert attachments == 0


async def test_deleting_server_cascades_attachment(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    server_id = await _seed_server(engine, community_id)
    factory = create_session_factory(engine)
    group = _group(community_id, [])

    async with ServersUnitOfWork(factory) as uow:
        await uow.groups.add(group)
        await uow.groups.attach(group.id, ServerId(server_id))
        await uow.commit()

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM server WHERE id = :id"), {"id": server_id})

    async with engine.connect() as conn:
        attachments = (
            await conn.execute(text("SELECT count(*) FROM server_group"))
        ).scalar_one()
    assert attachments == 0
    # The group itself survives the server delete.
    async with ServersUnitOfWork(factory) as uow:
        assert await uow.groups.get_by_id(group.id) is not None
