"""A hanging backup size probe keeps no pool connection checked out (#4107).

The backup listing sizes a legacy NULL-size row with a storage HEAD, which can
take as long as the store's timeouts and retries allow. The real unit of work's
session holds a pool connection from its first statement until its block exits,
so a HEAD made inside the block holds that connection for the whole wait. The
fakes only show that no block is open; this shows the pool itself on real
PostgreSQL. DB-gated: skipped unless ``MCD_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import QueuePool

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.backups import ListBackups
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from tests.integration.test_integrity_sweep import (  # noqa: F401
    _put_backup,
    _seed_server,
    engine,
)
from tests.storage.helpers import healthy_region_bytes

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.timezone.utc)


class _HangingSizeStore(StorageBackupStoreAdapter):
    """Waits for ``release`` before each size probe answers."""

    def __init__(self, *, storage: FsStorage) -> None:
        super().__init__(storage=storage)
        self.probing = asyncio.Event()
        self.release = asyncio.Event()

    async def size(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> int:
        self.probing.set()
        await self.release.wait()
        return await super().size(
            community_id=community_id, server_id=server_id, storage_ref=storage_ref
        )


async def test_a_listing_whose_size_probe_hangs_holds_no_pool_connection(
    engine: AsyncEngine,  # noqa: F811
    tmp_path: Path,
) -> None:
    community, server = await _seed_server(engine)
    factory = create_session_factory(engine)
    storage = FsStorage(tmp_path)
    ref = await _put_backup(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    row = Backup(
        id=BackupId.new(),
        server_id=server,
        storage_ref=ref,
        size_bytes=None,
        source=BackupSource.MANUAL,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW,
    )
    async with ServersUnitOfWork(factory) as uow:
        await uow.backups.add(row)
        await uow.commit()
    store = _HangingSizeStore(storage=storage)

    listing = asyncio.create_task(
        ListBackups(uow=ServersUnitOfWork(factory), backup_store=store)(
            community_id=community, server_id=server
        )
    )
    try:
        await store.probing.wait()
        assert isinstance(engine.pool, QueuePool)
        checked_out_while_probing = engine.pool.checkedout()
    finally:
        store.release.set()
    listed = await listing

    assert checked_out_while_probing == 0
    assert listed[0].backup.size_bytes is not None
    async with ServersUnitOfWork(factory) as uow:
        persisted = await uow.backups.get_by_id(row.id)
    assert persisted is not None
    assert persisted.size_bytes == listed[0].backup.size_bytes
