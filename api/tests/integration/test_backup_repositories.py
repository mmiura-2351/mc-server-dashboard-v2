"""Integration tests for the backup repository on PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). The schema is created and torn down per
test via the real 0001-0006 migrations so the adapter runs against the documented
shape (DATABASE.md Section 8). A community + server are seeded through the existing
adapters; backups are added/listed/deleted, and the server-delete cascade is
verified (a server's backups go with it).
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
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import (
    FakeClock,
    FakeFileStore,
    FakeVersionValidator,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


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


async def _seed_server(engine: AsyncEngine) -> uuid.UUID:
    community = Community(
        id=CommunityCommunityId(uuid.uuid4()),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    factory = create_session_factory(engine)
    async with CommunityUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.commit()
    server = await CreateServer(
        uow=ServersUnitOfWork(factory),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
    )(
        community_id=CommunityId(community.id.value),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="host_process",
        config={},
    )
    return server.id.value


def _backup(server_id: uuid.UUID, *, ref: str, created_at: dt.datetime) -> Backup:
    from mc_server_dashboard_api.servers.domain.value_objects import ServerId

    return Backup(
        id=BackupId.new(),
        server_id=ServerId(server_id),
        storage_ref=ref,
        size_bytes=123,
        source=BackupSource.MANUAL,
        created_by=None,
        created_at=created_at,
    )


async def test_add_list_newest_first_and_delete(engine: AsyncEngine) -> None:
    from mc_server_dashboard_api.servers.domain.value_objects import ServerId

    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    older = _backup(server_id, ref="a", created_at=_NOW)
    newer = _backup(server_id, ref="b", created_at=_NOW + dt.timedelta(hours=1))

    async with ServersUnitOfWork(factory) as uow:
        await uow.backups.add(older)
        await uow.backups.add(newer)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        listed = await uow.backups.list_for_server(ServerId(server_id))
    assert [b.id for b in listed] == [newer.id, older.id]
    assert listed[0].size_bytes == 123

    async with ServersUnitOfWork(factory) as uow:
        await uow.backups.delete(newer.id)
        await uow.commit()
    async with ServersUnitOfWork(factory) as uow:
        remaining = await uow.backups.list_for_server(ServerId(server_id))
    assert [b.id for b in remaining] == [older.id]


async def test_deleting_server_cascades_to_backups(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    async with ServersUnitOfWork(factory) as uow:
        await uow.backups.add(_backup(server_id, ref="a", created_at=_NOW))
        await uow.commit()

    # Delete the server row directly; the FK ON DELETE CASCADE removes its backups.
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM server WHERE id = :id"), {"id": server_id})
    async with engine.connect() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM backup WHERE server_id = :id"),
                {"id": server_id},
            )
        ).scalar_one()
    assert count == 0
