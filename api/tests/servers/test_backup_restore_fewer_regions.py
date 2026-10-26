"""Restore of a backup that holds fewer regions than the live world (issue #3446).

The missing-region gate (issue #854) refuses a snapshot that lost some of a
dimension's regions, and the restore route does not model that refusal, so a
restore that tripped it answered an untranslated 500. A restore is an
operator-chosen rollback and bypasses the gate. This drives the backup restore
ROUTE over the real restore use case, the real servers/storage seam and a real
:class:`FsStorage`, so what is pinned is the answer an operator gets.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI

from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.application.backups import RestoreBackup
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.servers import test_backup_endpoints as endpoints
from tests.servers.fakes import (
    FakeBackupRepository,
    FakeServerRepository,
    FakeUnitOfWork,
)
from tests.servers.test_backup_restore_link_archive import _at_rest_server
from tests.storage.helpers import drain, healthy_region_bytes, read_tar, tar_stream

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    endpoints._shared_app = shared_app


def test_restore_of_a_backup_with_fewer_regions_than_current_is_204(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    server = _at_rest_server()
    community = StorageCommunityId(server.community_id.value)
    scope = StorageServerId(server.id.value)
    backup_files = {"world/poi/r.0.0.mca": healthy_region_bytes()}

    async def _publish(files: dict[str, bytes]) -> None:
        handle = await storage.begin_snapshot(community, scope)
        await storage.write_snapshot(handle, tar_stream(files))
        await storage.commit_snapshot(handle)

    async def _arrange() -> str:
        await _publish(backup_files)
        key = await storage.create_backup_from_current(community, scope)
        # Two poi regions generated after the backup was taken.
        await _publish(
            {
                **backup_files,
                "world/poi/r.0.1.mca": healthy_region_bytes(),
                "world/poi/r.1.0.mca": healthy_region_bytes(),
            }
        )
        return key.value

    servers = FakeServerRepository()
    servers.seed(server)
    backups = FakeBackupRepository()
    backup = Backup(
        id=BackupId.new(),
        server_id=server.id,
        storage_ref=asyncio.run(_arrange()),
        size_bytes=None,
        source=BackupSource.MANUAL,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW,
    )
    backups.seed(backup)
    use_case = RestoreBackup(
        uow=FakeUnitOfWork(servers=servers, backups=backups),
        backup_store=StorageBackupStoreAdapter(storage=storage),
    )
    app = endpoints._app(
        member=True, allow=True, restore=cast(endpoints._FakeUseCase, use_case)
    )

    resp = endpoints._client(app).post(
        endpoints._url(
            server.community_id.value, server.id.value, f"/{backup.id.value}/restore"
        )
    )

    assert resp.status_code == 204
    published = asyncio.run(drain(storage.open_hydrate_source(community, scope)))
    assert read_tar(published) == backup_files
