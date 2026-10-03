"""The integrity sweep over the real object adapter, past a dead object (#2379).

``test_integrity_sweep.py`` drives the sweep against a fake archive store; this
wires it to :class:`StorageBackupStoreAdapter` over :class:`ObjectStorage` (on the
in-memory S3 fake) so the probe's damage-vs-outage classification is what decides
the row. An object whose every read delivers no byte used to classify as a store
outage, which the sweep deliberately propagates — so one such object aborted every
pass and no other backup in the deployment was checked again.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
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
from mc_server_dashboard_api.storage.adapters import object_store
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.value_objects import BackupKey
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.servers.fakes import (
    FakeBackupRepository,
    FakeServerRepository,
    FakeUnitOfWork,
)
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory
from tests.storage.helpers import healthy_region_bytes, region_targz

_NOW = dt.datetime(2026, 6, 9, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def _no_reprobe_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The re-read backoff is production pacing, not behaviour under test."""

    monkeypatch.setattr(object_store, "_REPROBE_BACKOFF_S", 0)


def _server(community_id: CommunityId) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=community_id,
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


def _backup(server: Server, storage_ref: str) -> Backup:
    return Backup(
        id=BackupId.new(),
        server_id=server.id,
        storage_ref=storage_ref,
        size_bytes=None,
        source=BackupSource.UPLOADED,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW,
    )


async def _store_archive(storage: ObjectStorage, server: Server, ref: str) -> str:
    archive = region_targz({"world/region/r.0.0.mca": healthy_region_bytes()})

    async def _stream() -> AsyncIterator[bytes]:
        yield archive

    community = StorageCommunityId(server.community_id.value)
    srv = StorageServerId(server.id.value)
    await storage.put_backup(community, srv, _stream(), BackupKey(ref))
    return storage._backup_key(community, srv, BackupKey(ref))


async def test_an_object_that_delivers_nothing_is_condemned_then_revised() -> None:
    s3 = FakeS3Store()
    storage = ObjectStorage(fake_s3_factory(s3))
    server = _server(CommunityId(uuid.uuid4()))
    dead = _backup(server, "dead")
    sound = _backup(server, "sound")
    dead_object = await _store_archive(storage, server, dead.storage_ref)
    await _store_archive(storage, server, sound.storage_ref)
    servers = FakeServerRepository()
    servers.seed(server)
    backups = FakeBackupRepository()
    backups.seed(dead)
    backups.seed(sound)
    uow = FakeUnitOfWork(servers=servers, backups=backups)
    sweep = IntegritySweep(
        uow=uow,
        backup_store=StorageBackupStoreAdapter(storage=storage),
        audit=RecordingAuditRecorder(),
    )
    # Every read of the dead object is torn down before its first byte.
    s3.read_aborts[dead_object] = [0, 0]

    first = await sweep()

    # The pass finishes: the dead object is condemned and the rest still checked.
    assert backups.by_id[dead.id].health is BackupHealth.UNREADABLE
    assert backups.by_id[sound.id].health is BackupHealth.HEALTHY
    assert first.backups_unreadable == 1

    # The store delivers the archive again (the read-abort queue is drained): the
    # next pass reads it back in full and lifts the verdict.
    await sweep()

    assert backups.by_id[dead.id].health is BackupHealth.HEALTHY
