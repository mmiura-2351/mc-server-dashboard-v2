"""Lock-adoption tests for the per-server lifecycle lock (issue #827, #1222).

The at-rest-gated use cases (RestoreBackup, the file mutations, UpdateServer,
DeleteServer, DeleteBackup, and the group file-sync helpers) check
``is_at_rest()`` in one transaction, mutate Storage over seconds-to-minutes, then
commit a second transaction. A start committed in that window operates on data
being mutated underneath it. These tests pin that each gated use case — and
StartServer's desired-state flip — takes the shared per-server
:class:`LifecycleLock` AROUND its work, so the lock serializes a start against a
gated operation.

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
    CreateBackup,
    DeleteBackup,
    PruneScheduledBackups,
    RestoreBackup,
)
from mc_server_dashboard_api.servers.application.files import (
    DeleteFile,
    MakeDir,
    RenameFile,
    RollbackFile,
    UploadFile,
    WriteFile,
)
from mc_server_dashboard_api.servers.application.groups import (
    AddPlayer,
    AttachGroup,
    DeleteGroup,
    DetachGroup,
    RemovePlayer,
)
from mc_server_dashboard_api.servers.application.lifecycle import StartServer
from mc_server_dashboard_api.servers.application.manage_server import (
    DeleteServer,
    UpdateServer,
)
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    SnapshotServer,
)
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.groups import (
    GroupId,
    GroupKind,
    GroupName,
    Player,
    PlayerGroup,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.servers.fakes import (
    FakeBackupArchiveStore,
    FakeBackupRepository,
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
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
        file_store=FakeFileStore(seed_eula=True),
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
        file_store=FakeFileStore(seed_eula=True),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id)

    assert lock.events == [
        (server.id, "acquire"),
        (server.id, "release"),
        (server.id, "acquire"),
        (server.id, "release"),
    ]


async def test_prune_and_restore_take_the_same_keyed_lock() -> None:
    # The retention prune (issue #1841) must serialize with a restore of a
    # candidate backup: both take the SAME per-server lock, so a backup can
    # never be deleted while a restore of it is in flight. (The actual blocking
    # is pinned against a real PostgreSQL advisory lock in
    # tests/integration/test_lifecycle_lock_concurrency.py.)
    server = _at_rest()
    server.backup_retention = {"keep_last": 1}
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
    await PruneScheduledBackups(
        uow=FakeUnitOfWork(servers=repo, backups=backups),
        backup_store=archive,
        audit=RecordingAuditRecorder(),
        clock=FakeClock(_NOW),
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


_ACQUIRE_RELEASE = "acquire", "release"


def _seeded() -> tuple[Server, FakeServerRepository, FakeLifecycleLock]:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    return server, repo, FakeLifecycleLock()


async def test_write_file_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    await WriteFile(
        uow=FakeUnitOfWork(servers=repo),
        control_plane=FakeControlPlane(),
        file_store=FakeFileStore(),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, rel_path="a.txt", content=b"x")

    assert lock.events == [(server.id, e) for e in _ACQUIRE_RELEASE]


async def test_rollback_file_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    await RollbackFile(
        uow=FakeUnitOfWork(servers=repo),
        file_store=FakeFileStore(),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, rel_path="a.txt", version_id="v1")

    assert lock.events == [(server.id, e) for e in _ACQUIRE_RELEASE]


async def test_upload_file_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    await UploadFile(
        uow=FakeUnitOfWork(servers=repo),
        file_store=FakeFileStore(),
        lifecycle_lock=lock,
    )(
        community_id=_COMMUNITY,
        server_id=server.id,
        dir_path="",
        filename="a.txt",
        content=b"x",
        extract=False,
    )

    assert lock.events == [(server.id, e) for e in _ACQUIRE_RELEASE]


async def test_delete_file_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    await DeleteFile(
        uow=FakeUnitOfWork(servers=repo),
        file_store=FakeFileStore(),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, rel_path="a.txt")

    assert lock.events == [(server.id, e) for e in _ACQUIRE_RELEASE]


async def test_make_dir_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    await MakeDir(
        uow=FakeUnitOfWork(servers=repo),
        file_store=FakeFileStore(),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, rel_path="d")

    assert lock.events == [(server.id, e) for e in _ACQUIRE_RELEASE]


async def test_rename_file_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.files["a.txt"] = b"x"
    # Rename onto itself: a no-op that only reads the source, so the at-rest path
    # runs without the fake's always-succeeds list_dir making the destination look
    # taken — the lock scope is what this asserts, not the move semantics.
    await RenameFile(
        uow=FakeUnitOfWork(servers=repo),
        file_store=store,
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, from_path="a.txt", to_path="a.txt")

    assert lock.events == [(server.id, e) for e in _ACQUIRE_RELEASE]


async def test_update_server_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()

    async def _allow(_perm: str) -> bool:
        return True

    await UpdateServer(
        uow=FakeUnitOfWork(servers=repo),
        clock=FakeClock(_NOW),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
        lifecycle_lock=lock,
    )(
        community_id=_COMMUNITY,
        server_id=server.id,
        name="renamed",
        authorize=_allow,
    )

    assert lock.events == [(server.id, e) for e in _ACQUIRE_RELEASE]


async def test_create_backup_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    archive = FakeBackupArchiveStore()
    uow = FakeUnitOfWork(servers=repo, backups=FakeBackupRepository())
    await CreateBackup(
        uow=uow,
        backup_store=archive,
        snapshot_server=SnapshotServer(uow=uow, control_plane=FakeControlPlane()),
        clock=FakeClock(_NOW),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, source=BackupSource.MANUAL)

    assert lock.events == [(server.id, e) for e in _ACQUIRE_RELEASE]


# --- Group file-sync use cases (issue #1222) --------------------------------


def _seed_group(uow: FakeUnitOfWork, *, kind: GroupKind = GroupKind.OP) -> PlayerGroup:
    group = PlayerGroup(
        id=GroupId.new(),
        community_id=_COMMUNITY,
        name=GroupName("admins"),
        kind=kind,
        players=[Player(uuid.uuid4(), "alice")],
    )
    uow.groups.seed(group)
    return group


async def test_attach_group_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    uow = FakeUnitOfWork(servers=repo)
    group = _seed_group(uow)
    await AttachGroup(uow=uow, file_store=FakeFileStore(), lifecycle_lock=lock)(
        community_id=_COMMUNITY, group_id=group.id, server_id=server.id
    )

    assert lock.events == [(server.id, e) for e in _ACQUIRE_RELEASE]


async def test_detach_group_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    uow = FakeUnitOfWork(servers=repo)
    group = _seed_group(uow)
    await uow.groups.attach(group.id, server.id)
    await DetachGroup(uow=uow, file_store=FakeFileStore(), lifecycle_lock=lock)(
        community_id=_COMMUNITY, group_id=group.id, server_id=server.id
    )

    assert lock.events == [(server.id, e) for e in _ACQUIRE_RELEASE]


async def test_add_player_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    uow = FakeUnitOfWork(servers=repo)
    group = _seed_group(uow)
    await uow.groups.attach(group.id, server.id)
    await AddPlayer(uow=uow, file_store=FakeFileStore(), lifecycle_lock=lock)(
        community_id=_COMMUNITY,
        group_id=group.id,
        player_uuid=uuid.uuid4(),
        username="bob",
    )

    assert lock.events == [(server.id, e) for e in _ACQUIRE_RELEASE]


async def test_remove_player_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    uow = FakeUnitOfWork(servers=repo)
    pid = uuid.uuid4()
    group = PlayerGroup(
        id=GroupId.new(),
        community_id=_COMMUNITY,
        name=GroupName("ops"),
        kind=GroupKind.OP,
        players=[Player(pid, "alice")],
    )
    uow.groups.seed(group)
    await uow.groups.attach(group.id, server.id)
    await RemovePlayer(uow=uow, file_store=FakeFileStore(), lifecycle_lock=lock)(
        community_id=_COMMUNITY, group_id=group.id, player_uuid=pid
    )

    assert lock.events == [(server.id, e) for e in _ACQUIRE_RELEASE]


async def test_delete_group_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    uow = FakeUnitOfWork(servers=repo)
    group = _seed_group(uow)
    await uow.groups.attach(group.id, server.id)
    await DeleteGroup(uow=uow, file_store=FakeFileStore(), lifecycle_lock=lock)(
        community_id=_COMMUNITY, group_id=group.id
    )

    assert lock.events == [(server.id, e) for e in _ACQUIRE_RELEASE]
