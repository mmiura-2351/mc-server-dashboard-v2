"""Lock-adoption tests for the per-server lifecycle lock (issue #827).

The at-rest-gated use cases (RestoreBackup, the file mutations, UpdateServer,
DeleteServer, DeleteBackup) check ``is_at_rest()`` in one transaction, mutate
Storage over seconds-to-minutes, then commit a second transaction. A start
committed in that window operates on data being mutated underneath it. These
tests pin that each gated use case — and StartServer's desired-state flip — takes
the shared per-server :class:`LifecycleLock` AROUND its work, so the lock
serializes a start against a gated operation.

The serialization itself (a start blocked until the gated op releases) is pinned
at the integration layer against a real PostgreSQL advisory lock in
``tests/integration/test_lifecycle_lock_concurrency.py``; here we use a recording
fake to assert the lock is taken at all and spans the right scope.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.servers.application.backups import (
    DeleteBackup,
    RestoreBackup,
)
from mc_server_dashboard_api.servers.application.lifecycle import StartServer
from mc_server_dashboard_api.servers.application.manage_server import DeleteServer
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
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from tests.servers.fakes import (
    FakeBackupArchiveStore,
    FakeBackupRepository,
    FakeClock,
    FakeControlPlane,
    FakeJarProvisioner,
    FakeLifecycleLock,
    FakeServerRepository,
    FakeStoreGenerationReader,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 11, 12, 0, tzinfo=dt.timezone.utc)
_COMMUNITY = CommunityId(uuid.uuid4())


def _at_rest() -> Server:
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=_COMMUNITY,
        name=ServerName("srv"),
        mc_edition="java",
        mc_version="1.21",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.CONTAINER,
        config={},
        game_port=25565,
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=_NOW,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _backup(server_id: ServerId) -> Backup:
    return Backup(
        id=BackupId.new(),
        server_id=server_id,
        storage_ref="ref",
        size_bytes=None,
        source=BackupSource.MANUAL,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW,
    )


async def test_restore_takes_lock_around_its_work() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    backup = _backup(server.id)
    backups.seed(backup)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    lock = FakeLifecycleLock()

    await RestoreBackup(uow=uow, backup_store=archive, lifecycle_lock=lock)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )

    assert lock.events == [(server.id, "acquire"), (server.id, "release")]


async def test_delete_server_takes_lock_around_its_work() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    archive = FakeBackupArchiveStore()
    lock = FakeLifecycleLock()

    await DeleteServer(uow=uow, backup_store=archive, lifecycle_lock=lock)(
        community_id=_COMMUNITY, server_id=server.id
    )

    assert lock.events == [(server.id, "acquire"), (server.id, "release")]


async def test_delete_backup_takes_lock_around_its_work() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    backup = _backup(server.id)
    backups.seed(backup)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    lock = FakeLifecycleLock()

    await DeleteBackup(uow=uow, backup_store=archive, lifecycle_lock=lock)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )

    assert lock.events == [(server.id, "acquire"), (server.id, "release")]


async def test_start_takes_lock_around_its_flip() -> None:
    server = _at_rest()
    worker = uuid.uuid4()
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    cp = FakeControlPlane(place_to=WorkerId(worker))
    lock = FakeLifecycleLock()

    await StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id)

    # The start takes and releases the lock; the release happens once the
    # desired-state flip has committed (before the post-commit dispatch).
    assert lock.events == [(server.id, "acquire"), (server.id, "release")]


async def test_start_and_restore_take_the_same_keyed_lock() -> None:
    # Both the start and the restore must take the SAME per-server lock for the
    # serialization to hold; assert each records an acquire/release on the same
    # lock keyed by the server id. (The actual blocking — start waits until the
    # restore releases — is pinned against a real PostgreSQL advisory lock in
    # tests/integration/test_lifecycle_lock_concurrency.py, where the lock can
    # genuinely block across connections.)
    server = _at_rest()
    worker = uuid.uuid4()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    backup = _backup(server.id)
    backups.seed(backup)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    lock = FakeLifecycleLock()

    await RestoreBackup(
        uow=FakeUnitOfWork(servers=repo, backups=backups),
        backup_store=archive,
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id)
    await StartServer(
        uow=FakeUnitOfWork(servers=repo),
        control_plane=FakeControlPlane(place_to=WorkerId(worker)),
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id)

    assert lock.events == [
        (server.id, "acquire"),
        (server.id, "release"),
        (server.id, "acquire"),
        (server.id, "release"),
    ]


@pytest.mark.parametrize("force", [False, True])
async def test_restore_releases_lock_on_corrupt(force: bool) -> None:
    # The lock must release even when the gated op raises mid-flight.
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    backup = _backup(server.id)
    backups.seed(backup)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    archive.corrupt_refs.add("ref")
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    lock = FakeLifecycleLock()

    if force:
        await RestoreBackup(uow=uow, backup_store=archive, lifecycle_lock=lock)(
            community_id=_COMMUNITY,
            server_id=server.id,
            backup_id=backup.id,
            force=True,
        )
    else:
        from mc_server_dashboard_api.servers.domain.errors import BackupCorruptError

        with pytest.raises(BackupCorruptError):
            await RestoreBackup(uow=uow, backup_store=archive, lifecycle_lock=lock)(
                community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
            )

    assert lock.events[-1] == (server.id, "release")
