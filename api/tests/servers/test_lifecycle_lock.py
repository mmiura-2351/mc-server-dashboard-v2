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


def _assert_around(
    events: list[tuple[ServerId, str]], server_id: ServerId, *work: str
) -> None:
    """Assert each ``work`` event fell strictly inside this server's lock hold.

    The property these tests are NAMED for -- the gated op does its work AROUND
    the lock, not merely takes and drops it (issue #2546, extending #2515). It
    holds only when the collaborating fake's ``events`` list is aliased onto the
    FakeLifecycleLock's, so the acquire, the recorded work, and the release all
    land on ONE timeline; asserting ``events == [acquire, release]`` alone can
    never see it -- the work could run before acquire or after release and stay
    green. Uses the ``events.index(...)`` idiom of
    ``tests/storage/test_crash_safety.py``: a work event that moved outside the
    hold (or an ordering reversed) reddens here, where the endpoint-only assert
    could not.
    """

    acquire_at = events.index((server_id, "acquire"))
    release_at = events.index((server_id, "release"))
    for label in work:
        assert (server_id, label) in events, (label, events)
        at = events.index((server_id, label))
        assert acquire_at < at < release_at, (label, events)


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
    # One timeline for the lock and the archive republish (issue #2546).
    archive.events = lock.events

    await RestoreBackup(uow=uow, backup_store=archive, lifecycle_lock=lock)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )

    _assert_around(lock.events, server.id, "restore")


async def test_delete_server_takes_lock_around_its_work() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    archive = FakeBackupArchiveStore()
    lock = FakeLifecycleLock()
    # One timeline for the lock, the working-set pack, and the row delete.
    repo.events = lock.events
    archive.events = lock.events

    await DeleteServer(uow=uow, backup_store=archive, lifecycle_lock=lock)(
        community_id=_COMMUNITY, server_id=server.id
    )

    _assert_around(lock.events, server.id, "prune-final", "delete-server")


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
    # One timeline for the lock, the archive delete, and the row delete.
    archive.events = lock.events
    backups.events = lock.events

    await DeleteBackup(uow=uow, backup_store=archive, lifecycle_lock=lock)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )

    _assert_around(lock.events, server.id, "delete-archive", "delete-row")


async def test_start_takes_lock_around_its_flip() -> None:
    server = _at_rest()
    worker = uuid.uuid4()
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    cp = FakeControlPlane(place_to=WorkerId(worker))
    lock = FakeLifecycleLock()
    # One timeline for the lock and the desired-state flip (issue #2546).
    repo.events = lock.events

    await StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
        file_store=FakeFileStore(seed_eula=True),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id)

    # The desired-state flip (the compare-and-set) happens strictly inside the
    # hold: the release fires once it has committed, before the post-commit
    # dispatch. A flip that ran outside the lock would still acquire/release.
    _assert_around(lock.events, server.id, "flip")


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


def _seeded() -> tuple[Server, FakeServerRepository, FakeLifecycleLock]:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    return server, repo, FakeLifecycleLock()


async def test_write_file_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    await WriteFile(
        uow=FakeUnitOfWork(servers=repo),
        control_plane=FakeControlPlane(),
        file_store=store,
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, rel_path="a.txt", content=b"x")

    _assert_around(lock.events, server.id, "write-file")


async def test_rollback_file_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    await RollbackFile(
        uow=FakeUnitOfWork(servers=repo),
        file_store=store,
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, rel_path="a.txt", version_id="v1")

    _assert_around(lock.events, server.id, "rollback")


async def test_upload_file_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    await UploadFile(
        uow=FakeUnitOfWork(servers=repo),
        file_store=store,
        lifecycle_lock=lock,
    )(
        community_id=_COMMUNITY,
        server_id=server.id,
        dir_path="",
        filename="a.txt",
        content=b"x",
        extract=False,
    )

    _assert_around(lock.events, server.id, "write-file")


async def test_delete_file_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    # Seed a member under "d" so it IS a directory and the delete lands on
    # delete_dir; the mutation is what must fall inside the hold, whichever branch
    # dispatches it. (The fake's list_dir used to answer every path, so this
    # landed on delete_dir for a path that was nothing at all; issue #2867.)
    store.files["d/a.txt"] = b"x"
    store.events = lock.events
    await DeleteFile(
        uow=FakeUnitOfWork(servers=repo),
        file_store=store,
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, rel_path="d")

    _assert_around(lock.events, server.id, "delete-dir")


async def test_make_dir_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    await MakeDir(
        uow=FakeUnitOfWork(servers=repo),
        file_store=store,
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, rel_path="d")

    _assert_around(lock.events, server.id, "make-dir")


async def test_rename_file_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.files["a.txt"] = b"x"
    store.events = lock.events
    # Rename onto itself: a no-op that only reads the source, so the lock scope is
    # what this asserts, not the move semantics. The work it reduces to is that
    # at-rest resolve read (list_dir via _path_is_dir, which misses on the seeded
    # FILE and confirms it from the stream), which must fall inside the hold or a
    # start could flip state under it.
    await RenameFile(
        uow=FakeUnitOfWork(servers=repo),
        file_store=store,
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, from_path="a.txt", to_path="a.txt")

    _assert_around(lock.events, server.id, "list-dir")


async def test_update_server_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    repo.events = lock.events

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

    _assert_around(lock.events, server.id, "update-row")


async def test_create_backup_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    archive = FakeBackupArchiveStore()
    archive.events = lock.events
    uow = FakeUnitOfWork(servers=repo, backups=FakeBackupRepository())
    await CreateBackup(
        uow=uow,
        backup_store=archive,
        snapshot_server=SnapshotServer(uow=uow, control_plane=FakeControlPlane()),
        clock=FakeClock(_NOW),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, source=BackupSource.MANUAL)

    _assert_around(lock.events, server.id, "create-archive")


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
    store = FakeFileStore()
    store.events = lock.events
    uow = FakeUnitOfWork(servers=repo)
    group = _seed_group(uow)
    await AttachGroup(uow=uow, file_store=store, lifecycle_lock=lock)(
        community_id=_COMMUNITY, group_id=group.id, server_id=server.id
    )

    _assert_around(lock.events, server.id, "write-file")


async def test_detach_group_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    uow = FakeUnitOfWork(servers=repo)
    group = _seed_group(uow)
    await uow.groups.attach(group.id, server.id)
    await DetachGroup(uow=uow, file_store=store, lifecycle_lock=lock)(
        community_id=_COMMUNITY, group_id=group.id, server_id=server.id
    )

    _assert_around(lock.events, server.id, "write-file")


async def test_add_player_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    uow = FakeUnitOfWork(servers=repo)
    group = _seed_group(uow)
    await uow.groups.attach(group.id, server.id)
    await AddPlayer(uow=uow, file_store=store, lifecycle_lock=lock)(
        community_id=_COMMUNITY,
        group_id=group.id,
        player_uuid=uuid.uuid4(),
        username="bob",
    )

    _assert_around(lock.events, server.id, "write-file")


async def test_remove_player_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
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
    await RemovePlayer(uow=uow, file_store=store, lifecycle_lock=lock)(
        community_id=_COMMUNITY, group_id=group.id, player_uuid=pid
    )

    _assert_around(lock.events, server.id, "write-file")


async def test_delete_group_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    uow = FakeUnitOfWork(servers=repo)
    group = _seed_group(uow)
    await uow.groups.attach(group.id, server.id)
    await DeleteGroup(uow=uow, file_store=store, lifecycle_lock=lock)(
        community_id=_COMMUNITY, group_id=group.id
    )

    _assert_around(lock.events, server.id, "write-file")
