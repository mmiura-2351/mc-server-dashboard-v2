"""Restore of a backup archive that holds a link member, end to end (issue #3371).

The fs store refuses a hard-link or symlink member at restore, and its own backup
writer could put one in an archive before it stopped emitting them. These tests
drive the backup restore ROUTE over the real restore use case, the real
servers/storage seam and a real :class:`FsStorage`, so what is pinned is the
answer an operator gets — the modelled 409 ``backup_unreadable``, not a 500 —
with the live snapshot left as it was and the verdict recorded.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import tarfile
import uuid
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI

from mc_server_dashboard_api.audit.domain.events import Outcome
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
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.servers import test_backup_endpoints as endpoints
from tests.servers.fakes import (
    FakeBackupRepository,
    FakeServerRepository,
    FakeUnitOfWork,
)
from tests.storage.helpers import (
    drain,
    read_tar,
    stream_of,
    tar_stream,
    tar_with_link_member,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    endpoints._shared_app = shared_app


def _at_rest_server() -> Server:
    return Server(
        id=ServerId.new(),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.mark.parametrize(
    "link_type", [tarfile.LNKTYPE, tarfile.SYMTYPE], ids=["hard-link", "symlink"]
)
@pytest.mark.parametrize("force", [False, True])
def test_restore_of_an_archive_holding_a_link_is_409_and_keeps_the_snapshot(
    tmp_path: Path, link_type: bytes, force: bool
) -> None:
    storage = FsStorage(tmp_path)
    server = _at_rest_server()
    community = StorageCommunityId(server.community_id.value)
    scope = StorageServerId(server.id.value)

    async def _arrange() -> str:
        handle = await storage.begin_snapshot(community, scope)
        await storage.write_snapshot(handle, tar_stream({"keep.txt": b"prior"}))
        await storage.commit_snapshot(handle)
        key = await storage.put_backup(
            community, scope, stream_of(tar_with_link_member(link_type, mode="w:gz"))
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
    recorder = RecordingAuditRecorder()
    app = endpoints._app(
        member=True,
        allow=True,
        restore=cast(endpoints._FakeUseCase, use_case),
        recorder=recorder,
    )

    resp = endpoints._client(app).post(
        endpoints._url(
            server.community_id.value, server.id.value, f"/{backup.id.value}/restore"
        ),
        params={"force": str(force).lower()},
    )

    assert resp.status_code == 409
    assert resp.json()["reason"] == "backup_unreadable"
    assert [e.outcome for e in recorder.events] == [Outcome.DENIED]
    # The live snapshot is the one from before, and the verdict is recorded, so
    # the next attempt is refused without reading the archive again.
    published = asyncio.run(drain(storage.open_hydrate_source(community, scope)))
    assert read_tar(published) == {"keep.txt": b"prior"}
    assert backups.by_id[backup.id].health is BackupHealth.UNREADABLE
