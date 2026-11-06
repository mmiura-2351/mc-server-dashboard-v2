"""A backup archive no restore can extract, end to end on both backends (#3237).

Two archives read back as a well-formed ``tar.gz`` and still hold nothing a
restore can publish: one with a member escaping the extraction root
(``../escape.txt``), and one inflating past the restore decompressed-size cap
(a gzip bomb). Neither outcome can be overridden, so both are the
not-restorable verdict. These tests drive the restore ROUTE and the integrity
sweep over the real use cases, the real servers/storage seam and a real
:class:`FsStorage` / :class:`ObjectStorage`, so what is pinned is the answer an
operator gets: the modelled 409 ``backup_unreadable`` rather than a 500, the
verdict ``UNREADABLE`` recorded, and a sweep that goes on to the next backup.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
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
from mc_server_dashboard_api.servers.application.integrity_sweep import IntegritySweep
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
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.port import Storage
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
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory
from tests.storage.helpers import (
    bomb_targz,
    drain,
    malicious_tar_with_escape,
    read_tar,
    stream_of,
    tar_bytes,
    tar_stream,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)

# Small enough that the bomb fixture trips it, large enough for every other one.
_MAX_RESTORE_BYTES = 1024

_ARCHIVES = {
    "escaping-member": lambda: gzip.compress(malicious_tar_with_escape()),
    "over-limit": bomb_targz,
}


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    endpoints._shared_app = shared_app


@pytest.fixture(params=["fs", "object"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Storage:
    if request.param == "fs":
        return FsStorage(tmp_path, max_restore_bytes=_MAX_RESTORE_BYTES)
    return ObjectStorage(
        fake_s3_factory(FakeS3Store()), max_restore_bytes=_MAX_RESTORE_BYTES
    )


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


def _scope(server: Server) -> tuple[StorageCommunityId, StorageServerId]:
    return StorageCommunityId(server.community_id.value), StorageServerId(
        server.id.value
    )


def _backup(server: Server, storage_ref: str, *, created_at: dt.datetime) -> Backup:
    return Backup(
        id=BackupId.new(),
        server_id=server.id,
        storage_ref=storage_ref,
        size_bytes=None,
        source=BackupSource.UPLOADED,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=created_at,
    )


async def _put(storage: Storage, server: Server, archive: bytes) -> str:
    community, scope = _scope(server)
    key = await storage.put_backup(community, scope, stream_of(archive))
    return key.value


@pytest.mark.parametrize("archive", list(_ARCHIVES))
@pytest.mark.parametrize("force", [False, True])
def test_restore_of_an_unrestorable_archive_is_409_and_keeps_the_snapshot(
    storage: Storage, archive: str, force: bool
) -> None:
    server = _at_rest_server()
    community, scope = _scope(server)

    async def _arrange() -> str:
        handle = await storage.begin_snapshot(community, scope)
        await storage.write_snapshot(handle, tar_stream({"keep.txt": b"prior"}))
        await storage.commit_snapshot(handle)
        return await _put(storage, server, _ARCHIVES[archive]())

    servers = FakeServerRepository()
    servers.seed(server)
    backups = FakeBackupRepository()
    backup = _backup(server, asyncio.run(_arrange()), created_at=_NOW)
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
    published = asyncio.run(drain(storage.open_hydrate_source(community, scope)))
    assert read_tar(published) == {"keep.txt": b"prior"}
    assert backups.by_id[backup.id].health is BackupHealth.UNREADABLE


@pytest.mark.parametrize("archive", list(_ARCHIVES))
async def test_sweep_records_an_unrestorable_archive_and_checks_the_next(
    storage: Storage, archive: str
) -> None:
    server = _at_rest_server()
    # The sweep lists newest first, so the unrestorable backup is checked first.
    bad = _backup(
        server,
        await _put(storage, server, _ARCHIVES[archive]()),
        created_at=_NOW + dt.timedelta(minutes=1),
    )
    sound = _backup(
        server,
        await _put(storage, server, gzip.compress(tar_bytes({"keep.txt": b"ok"}))),
        created_at=_NOW,
    )
    servers = FakeServerRepository()
    servers.seed(server)
    backups = FakeBackupRepository()
    backups.seed(bad)
    backups.seed(sound)
    sweep = IntegritySweep(
        uow=FakeUnitOfWork(servers=servers, backups=backups),
        backup_store=StorageBackupStoreAdapter(storage=storage),
        audit=RecordingAuditRecorder(),
    )

    summary = await sweep()

    assert backups.by_id[bad.id].health is BackupHealth.UNREADABLE
    assert backups.by_id[sound.id].health is BackupHealth.HEALTHY
    assert summary.backups_unreadable == 1
    assert summary.backups_healthy == 1
