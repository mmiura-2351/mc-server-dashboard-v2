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
from dataclasses import replace as dc_replace

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
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
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
        port_range=PortRange(start=25565, end=25664),
    )(
        community_id=CommunityId(community.id.value),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        config={},
    )
    return server.id.value


def _backup(
    server_id: uuid.UUID,
    *,
    ref: str,
    created_at: dt.datetime,
    health: BackupHealth = BackupHealth.HEALTHY,
) -> Backup:
    from mc_server_dashboard_api.servers.domain.value_objects import ServerId

    return Backup(
        id=BackupId.new(),
        server_id=ServerId(server_id),
        storage_ref=ref,
        size_bytes=123,
        source=BackupSource.MANUAL,
        health=health,
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


async def test_list_breaks_created_at_ties_by_id_descending(
    engine: AsyncEngine,
) -> None:
    # Deterministic ordering (#777 review): two backups with the SAME created_at
    # must list in a stable order so the retained "newest" head is deterministic.
    # The secondary id-desc tie-break pins it.
    from mc_server_dashboard_api.servers.domain.value_objects import ServerId

    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    lo = _backup(server_id, ref="lo", created_at=_NOW)
    hi = _backup(server_id, ref="hi", created_at=_NOW)
    # Pin the relative id order so the assertion is deterministic regardless of
    # which UUIDs new() minted.
    lo_id, hi_id = sorted([lo.id, hi.id], key=lambda b: b.value)
    lo = dc_replace(lo, id=lo_id)
    hi = dc_replace(hi, id=hi_id)

    async with ServersUnitOfWork(factory) as uow:
        await uow.backups.add(lo)
        await uow.backups.add(hi)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        listed = await uow.backups.list_for_server(ServerId(server_id))
    # Equal created_at -> the higher id sorts first (id desc tie-break).
    assert [b.id for b in listed] == [hi_id, lo_id]


async def test_health_round_trips(engine: AsyncEngine) -> None:
    """The ``health`` field persists and reads back unchanged (issue #742)."""

    from mc_server_dashboard_api.servers.domain.value_objects import ServerId

    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    healthy = _backup(server_id, ref="h", created_at=_NOW, health=BackupHealth.HEALTHY)
    unknown = _backup(
        server_id,
        ref="u",
        created_at=_NOW + dt.timedelta(hours=1),
        health=BackupHealth.UNKNOWN,
    )

    async with ServersUnitOfWork(factory) as uow:
        await uow.backups.add(healthy)
        await uow.backups.add(unknown)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        listed = await uow.backups.list_for_server(ServerId(server_id))
        fetched = await uow.backups.get_by_id(healthy.id)
    assert {b.id: b.health for b in listed} == {
        unknown.id: BackupHealth.UNKNOWN,
        healthy.id: BackupHealth.HEALTHY,
    }
    assert fetched is not None
    assert fetched.health is BackupHealth.HEALTHY


async def test_update_health_sets_quarantined(engine: AsyncEngine) -> None:
    """``update_health`` rewrites just the health column (the restore gate, #743)."""

    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    backup = _backup(server_id, ref="q", created_at=_NOW, health=BackupHealth.HEALTHY)

    async with ServersUnitOfWork(factory) as uow:
        await uow.backups.add(backup)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        await uow.backups.update_health(backup.id, BackupHealth.QUARANTINED)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        fetched = await uow.backups.get_by_id(backup.id)
    assert fetched is not None
    assert fetched.health is BackupHealth.QUARANTINED
    # Only the health changed; the rest of the row is intact.
    assert fetched.storage_ref == "q"


async def test_update_size_backfills_null_row(engine: AsyncEngine) -> None:
    """``update_size`` rewrites just the size column on a legacy NULL row (#661)."""

    server_id = await _seed_server(engine)
    factory = create_session_factory(engine)
    legacy = dc_replace(
        _backup(server_id, ref="legacy", created_at=_NOW), size_bytes=None
    )

    async with ServersUnitOfWork(factory) as uow:
        await uow.backups.add(legacy)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        await uow.backups.update_size(legacy.id, 4096)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        fetched = await uow.backups.get_by_id(legacy.id)
    assert fetched is not None
    assert fetched.size_bytes == 4096
    # Only the size changed; the rest of the row is intact.
    assert fetched.storage_ref == "legacy"


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
