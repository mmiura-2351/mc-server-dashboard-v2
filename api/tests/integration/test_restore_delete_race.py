"""A server delete cannot prune between a restore's copy and its flip (issue #3904).

A restore fills its new snapshot without the per-server storage lock, then takes
that lock to flip onto it. The object adapter copies the staged backup into a
fresh ``snapshots/<id>/`` prefix and flips with no re-check. The fs adapter
extracts it into ``incoming/`` and moves and flips it under the lock.
``DeleteServer``'s prune deletes ``snapshots/`` and ``incoming/``, so on the
object backend a prune landing in that window would leave the flip pointing at
an emptied prefix. What keeps it out is the caller: ``RestoreBackup`` and
``DeleteServer`` hold the per-server lifecycle lock across their whole storage
call, so a delete arriving in the window is refused as busy and prunes nothing.

Each test holds the restore at the end of its lock-free work, runs a delete
against the real PostgreSQL advisory lock with a short acquire budget, then lets
the restore flip. No test sleeps.

DB-gated: skipped unless ``MCD_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
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
from mc_server_dashboard_api.servers.adapters import (
    lifecycle_lock as lifecycle_lock_mod,
)
from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.lifecycle_lock import PgLifecycleLock
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.backups import (
    RestoreBackup,
    RestoreResult,
)
from mc_server_dashboard_api.servers.application.manage_server import (
    CreateServer,
    DeleteServer,
)
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.errors import ServerBusyError
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import (
    ObjectStorage,
    S3Client,
    S3Object,
)
from mc_server_dashboard_api.storage.domain.port import Storage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from mc_server_dashboard_api.storage.integrity.region import WorkingSetReport
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import FakeClock, FakeFileStore, FakeVersionValidator
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory
from tests.storage.helpers import drain, read_tar, tar_stream

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)

BACKED_UP = {"level.dat": b"BACKED-UP"}
LIVE = {"level.dat": b"LIVE"}


class _Hold:
    """Holds a restore at the end of its lock-free work, once armed."""

    def __init__(self) -> None:
        self.armed = False
        self.reached = asyncio.Event()
        self.resume = asyncio.Event()

    async def __call__(self) -> None:
        if self.armed:
            self.armed = False
            self.reached.set()
            await self.resume.wait()


class _HeldObjectStorage(ObjectStorage):
    """Holds once the copy into the new snapshot is done, before the flip's lock."""

    def __init__(self, hold: _Hold) -> None:
        super().__init__(fake_s3_factory(FakeS3Store()))
        self._hold = hold

    async def _copy_into_snapshot(
        self,
        client: S3Client,
        new_prefix: str,
        staged_prefix: str,
        staged: list[S3Object],
    ) -> None:
        await super()._copy_into_snapshot(client, new_prefix, staged_prefix, staged)
        await self._hold()


class _HeldFsStorage(FsStorage):
    """Holds once the staging is extracted and checked, before the move's lock."""

    def __init__(self, root: Path, hold: _Hold) -> None:
        super().__init__(root)
        self._hold = hold

    async def _publish_restore(
        self,
        community_id: StorageCommunityId,
        server_id: StorageServerId,
        staging: Path,
        report: WorkingSetReport,
    ) -> None:
        await self._hold()
        await super()._publish_restore(community_id, server_id, staging, report)


def _object_storage(tmp_path: Path, hold: _Hold) -> Storage:
    del tmp_path
    return _HeldObjectStorage(hold)


def _fs_storage(tmp_path: Path, hold: _Hold) -> Storage:
    return _HeldFsStorage(tmp_path, hold)


@pytest.fixture
async def engine(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncEngine]:
    # The delete's acquire gives up while the held restore still has the lock.
    monkeypatch.setattr(lifecycle_lock_mod, "_ACQUIRE_BUDGET_SECONDS", 0.3)
    monkeypatch.setattr(lifecycle_lock_mod, "_ACQUIRE_POLL_SECONDS", 0.05)
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


async def _publish(
    storage: Storage,
    community: StorageCommunityId,
    server: StorageServerId,
    files: dict[str, bytes],
) -> None:
    base = await storage.current_generation(community, server)
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(files))
    await storage.commit_snapshot(handle, expected_base=base)


# 20 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(20 * 10 + 30)
@pytest.mark.parametrize(
    "make_storage", [_fs_storage, _object_storage], ids=["fs", "object"]
)
async def test_server_delete_between_a_restore_copy_and_its_flip_is_refused(
    engine: AsyncEngine,
    tmp_path: Path,
    make_storage: Callable[[Path, _Hold], Storage],
) -> None:
    factory = create_session_factory(engine)
    community = Community(
        id=CommunityCommunityId(uuid.uuid4()),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with CommunityUnitOfWork(factory) as community_uow:
        await community_uow.communities.add(community)
        await community_uow.commit()
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
    hold = _Hold()
    storage = make_storage(tmp_path, hold)
    scope = (StorageCommunityId(community.id.value), StorageServerId(server.id.value))
    await _publish(storage, *scope, BACKED_UP)
    key = await storage.create_backup_from_current(*scope)
    await _publish(storage, *scope, LIVE)
    backup_id = BackupId.new()
    async with ServersUnitOfWork(factory) as uow:
        await uow.backups.add(
            Backup(
                id=backup_id,
                server_id=server.id,
                storage_ref=key.value,
                size_bytes=None,
                source=BackupSource.MANUAL,
                health=BackupHealth.HEALTHY,
                created_by=None,
                created_at=_NOW,
            )
        )
        await uow.commit()
    backup_store = StorageBackupStoreAdapter(storage=storage)
    lock = PgLifecycleLock(engine=engine)

    hold.armed = True
    restore = asyncio.create_task(
        RestoreBackup(
            uow=ServersUnitOfWork(factory),
            backup_store=backup_store,
            lifecycle_lock=lock,
        )(community_id=server.community_id, server_id=server.id, backup_id=backup_id)
    )
    await hold.reached.wait()
    try:
        with pytest.raises(ServerBusyError):
            await DeleteServer(
                uow=ServersUnitOfWork(factory),
                backup_store=backup_store,
                lifecycle_lock=lock,
            )(community_id=server.community_id, server_id=server.id)
    finally:
        hold.resume.set()
        await asyncio.wait({restore})

    assert restore.result() == RestoreResult(forced_corrupt=False, corrupt_count=0)
    hydrated = read_tar(await drain(storage.open_hydrate_source(*scope)))
    assert hydrated == BACKED_UP
