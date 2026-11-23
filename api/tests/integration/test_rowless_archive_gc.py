"""The row-less backup archive reclaim against PostgreSQL + FsStorage (#4106).

The unit tests (``tests/servers/test_rowless_archive_gc.py``) pin the pass
against fakes. These run it over the real adapters: the archives' mtimes from
the fs listing, the backup rows from PostgreSQL, and the real advisory lock a
lifecycle operation in flight holds. DB-gated: skipped unless
``MCD_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.servers.adapters import (
    lifecycle_lock as lifecycle_lock_mod,
)
from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.clock import SystemClock
from mc_server_dashboard_api.servers.adapters.lifecycle_lock import PgLifecycleLock
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.rowless_archive_gc import (
    ReclaimRowlessBackupArchives,
)
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.integration.test_integrity_sweep import (  # noqa: F401
    _put_backup,
    _seed_server,
    engine,
)
from tests.storage.helpers import healthy_region_bytes
from tests.store_ownership import FakeStoreOwnership

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_WORLD = {"world/region/r.0.0.mca": healthy_region_bytes()}
_GRACE = dt.timedelta(hours=1)


def _backdate(root: Path, community: CommunityId, server: ServerId, ref: str) -> None:
    archive = (
        root
        / "communities"
        / str(community.value)
        / "servers"
        / str(server.value)
        / "backups"
        / f"{ref}.tar.gz"
    )
    old = time.time() - 2 * _GRACE.total_seconds()
    os.utime(archive, (old, old))


async def _refs(
    storage: FsStorage, community: CommunityId, server: ServerId
) -> set[str]:
    keys = await storage.list_backups(
        StorageCommunityId(community.value), StorageServerId(server.value)
    )
    return {key.value for key in keys}


def _reclaim(engine: AsyncEngine, storage: FsStorage) -> ReclaimRowlessBackupArchives:  # noqa: F811
    return ReclaimRowlessBackupArchives(
        uow=ServersUnitOfWork(create_session_factory(engine)),
        backup_store=StorageBackupStoreAdapter(storage=storage),
        clock=SystemClock(),
        ownership=FakeStoreOwnership(),
        lifecycle_lock=PgLifecycleLock(engine=engine),
        grace=_GRACE,
    )


# Three archive writes fsync on the fs backend, at 10 s each plus 30 s (#3651).
@pytest.mark.timeout(3 * 6 * 10 + 30)
async def test_reclaims_only_the_old_archive_no_row_names(
    engine: AsyncEngine,  # noqa: F811
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = await _seed_server(engine)
    named = await _put_backup(storage, community, server, _WORLD)
    leftover = await _put_backup(storage, community, server, _WORLD)
    young = await _put_backup(storage, community, server, _WORLD)
    _backdate(tmp_path, community, server, named)
    _backdate(tmp_path, community, server, leftover)
    async with ServersUnitOfWork(create_session_factory(engine)) as uow:
        await uow.backups.add(
            Backup(
                id=BackupId.new(),
                server_id=server,
                storage_ref=named,
                size_bytes=None,
                source=BackupSource.MANUAL,
                health=BackupHealth.HEALTHY,
                created_by=None,
                created_at=dt.datetime.now(dt.UTC),
            )
        )
        await uow.commit()

    await _reclaim(engine, storage)()

    assert await _refs(storage, community, server) == {named, young}


@pytest.mark.timeout(6 * 10 + 30)
async def test_a_server_another_operation_holds_keeps_its_leftover(
    engine: AsyncEngine,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lifecycle_lock_mod, "_ACQUIRE_BUDGET_SECONDS", 0.5)
    storage = FsStorage(tmp_path)
    community, server = await _seed_server(engine)
    leftover = await _put_backup(storage, community, server, _WORLD)
    _backdate(tmp_path, community, server, leftover)

    async with PgLifecycleLock(engine=engine).hold(server):
        await _reclaim(engine, storage)()

    assert await _refs(storage, community, server) == {leftover}
    await _reclaim(engine, storage)()
    assert await _refs(storage, community, server) == set()
