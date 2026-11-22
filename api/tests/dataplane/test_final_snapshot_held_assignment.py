"""At-rest writes and the final snapshot of a stop that still holds its Worker.

A stop keeps the server assigned until its final snapshot settles (#869). That
snapshot declares the base its scratch was hydrated from, and the publish guard
lets the assigned Worker through even when an API sentinel published ``current``
since (the #1703 exemption). So an at-rest restore or edit made while the row is
still assigned is overwritten by the snapshot that follows it (issue #3998). The
fix is at the doors: every at-rest write refuses a row that is still assigned.

These run the doors over the real storage adapters on both backends, then post
the Worker's final snapshot through the real data plane, and check that the door
refused, the store did not move, and the Worker's world is the one published.

The exemption itself stays, for its live case: an instance a start adopted
instead of hydrating (#3865) runs behind the sentinel ``current`` that start's
own EULA or group write stamped, and must still publish.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import tarfile
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mc_server_dashboard_api.dependencies import (
    WorkerAssignment,
    get_assigned_worker_lookup,
    get_resolved_jar_lookup,
    get_storage,
    get_transfer_semaphore,
    get_worker_credential,
)
from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.file_store import StorageFileStoreAdapter
from mc_server_dashboard_api.servers.application.backups import RestoreBackup
from mc_server_dashboard_api.servers.application.files import WriteFile
from mc_server_dashboard_api.servers.application.lifecycle import StopServer
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import ServerAssignedError
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId as ServersCommunityId,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    DesiredState,
    FinalSnapshotRefusal,
    ObservedState,
    ServerName,
    ServerType,
    WorkerId,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    ServerId as ServersServerId,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.port import API_EDIT_PUBLISHER, Storage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    RelPath,
    ServerId,
)
from tests.servers.fakes import (
    FakeClock,
    FakeControlPlane,
    FakePluginCacheStore,
    FakeUnitOfWork,
)
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory

_CREDENTIAL = "test-worker-credential"
_NOW = dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.timezone.utc)

# Every publish, backup and restore on the fs backend fsyncs (issue #3651).
pytestmark = pytest.mark.timeout(300)


def _fs_storage(tmp_path: Path) -> Storage:
    return FsStorage(tmp_path)


def _object_storage(tmp_path: Path) -> Storage:
    del tmp_path
    return ObjectStorage(fake_s3_factory(FakeS3Store()))


_BACKENDS = pytest.mark.parametrize(
    "make_storage", [_fs_storage, _object_storage], ids=["fs", "object"]
)


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel, content in files.items():
            info = tarfile.TarInfo(name=rel)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _read_tar(blob: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tar:
        for member in tar.getmembers():
            if member.isfile():
                handle = tar.extractfile(member)
                assert handle is not None
                out[member.name] = handle.read()
    return out


@pytest.fixture
def data_plane(shared_app: FastAPI) -> Iterator[Callable[[Storage, str], TestClient]]:
    """The data plane over ``storage``, with ``worker`` the server's assigned Worker.

    The assignment reads as hydrated (no ``awaiting_hydrate`` mark): the Worker
    that ran the server and is now uploading its final snapshot, or one whose
    instance a start adopted.
    """

    def _bind(storage: Storage, worker: str) -> TestClient:
        shared_app.dependency_overrides[get_storage] = lambda: storage

        async def _no_jar(_c: uuid.UUID, _s: uuid.UUID) -> str | None:
            return None

        async def _assigned(_c: uuid.UUID, _s: uuid.UUID) -> WorkerAssignment:
            return WorkerAssignment(worker_id=worker, awaiting_hydrate=False)

        shared_app.dependency_overrides[get_resolved_jar_lookup] = lambda: _no_jar
        shared_app.dependency_overrides[get_assigned_worker_lookup] = lambda: _assigned
        shared_app.dependency_overrides[get_worker_credential] = lambda: _CREDENTIAL
        semaphore = asyncio.Semaphore(1)
        shared_app.dependency_overrides[get_transfer_semaphore] = lambda: semaphore
        return TestClient(shared_app)

    yield _bind


async def _publish(
    storage: Storage, c: CommunityId, s: ServerId, files: dict[str, bytes], worker: str
) -> None:
    handle = await storage.begin_snapshot(c, s)

    async def _stream() -> AsyncIterator[bytes]:
        yield _tar_bytes(files)

    await storage.write_snapshot(handle, _stream())
    await storage.commit_snapshot(handle, publisher=worker)


async def _live(storage: Storage, c: CommunityId, s: ServerId) -> dict[str, bytes]:
    return _read_tar(
        b"".join([chunk async for chunk in storage.open_hydrate_source(c, s)])
    )


def _post_snapshot(
    client: TestClient,
    c: CommunityId,
    s: ServerId,
    *,
    base: int,
    worker: str,
    files: dict[str, bytes],
) -> int:
    with client:
        resp = client.post(
            f"/api/data-plane/communities/{c.value}/servers/{s.value}/snapshot",
            content=_tar_bytes(files),
            headers={
                "Authorization": f"Bearer {_CREDENTIAL}",
                "X-Working-Set-Base-Generation": str(base),
                "X-Worker-Id": worker,
            },
        )
    return resp.status_code


def _held_server(c: CommunityId, s: ServerId, worker: str) -> Server:
    """The row in the window: stopped, observed at rest, still assigned."""

    return Server(
        id=ServersServerId(s.value),
        community_id=ServersCommunityId(c.value),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=_NOW,
        assigned_worker_id=WorkerId(uuid.UUID(worker)),
        created_at=_NOW,
        updated_at=_NOW,
    )


@_BACKENDS
def test_a_restore_during_the_final_snapshot_is_refused_and_the_snapshot_lands(
    tmp_path: Path,
    make_storage: Callable[[Path], Storage],
    data_plane: Callable[[Storage, str], TestClient],
) -> None:
    worker = str(uuid.uuid4())
    storage = make_storage(tmp_path)
    c, s = CommunityId(uuid.uuid4()), ServerId(uuid.uuid4())
    sc, ss = ServersCommunityId(c.value), ServersServerId(s.value)
    backup_store = StorageBackupStoreAdapter(storage=storage)

    # The Worker published g1, which was backed up, then ran on to g2: its
    # scratch is at the g2 generation when the stop begins.
    asyncio.run(_publish(storage, c, s, {"k": b"g1"}, worker))
    storage_ref = uuid.uuid4().hex
    asyncio.run(
        backup_store.create_from_current(
            community_id=sc, server_id=ss, storage_ref=storage_ref
        )
    )
    asyncio.run(_publish(storage, c, s, {"k": b"g2"}, worker))
    base = asyncio.run(storage.current_generation(c, s))

    uow = FakeUnitOfWork()
    uow.servers.seed(_held_server(c, s, worker))
    backup = Backup(
        id=BackupId.new(),
        server_id=ss,
        storage_ref=storage_ref,
        size_bytes=None,
        source=BackupSource.MANUAL,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW,
    )
    uow.backups.seed(backup)
    restore = RestoreBackup(
        uow=uow,
        backup_store=backup_store,
        file_store=StorageFileStoreAdapter(storage=storage),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )

    # The operator restores while the final snapshot is still in flight.
    with pytest.raises(ServerAssignedError):
        asyncio.run(restore(community_id=sc, server_id=ss, backup_id=backup.id))

    assert asyncio.run(storage.current_generation(c, s)) == base
    assert asyncio.run(storage.current_publisher(c, s)) == worker
    assert asyncio.run(_live(storage, c, s)) == {"k": b"g2"}

    # The Worker's final snapshot then commits normally, and its world is live.
    status = _post_snapshot(
        data_plane(storage, worker),
        c,
        s,
        base=base,
        worker=worker,
        files={"k": b"final-snapshot"},
    )
    assert status == 204
    assert asyncio.run(storage.current_generation(c, s)) == base + 1
    assert asyncio.run(storage.current_publisher(c, s)) == worker
    assert asyncio.run(_live(storage, c, s)) == {"k": b"final-snapshot"}


@_BACKENDS
def test_an_edit_during_the_final_snapshot_is_refused_and_the_snapshot_lands(
    tmp_path: Path,
    make_storage: Callable[[Path], Storage],
    data_plane: Callable[[Storage, str], TestClient],
) -> None:
    worker = str(uuid.uuid4())
    storage = make_storage(tmp_path)
    c, s = CommunityId(uuid.uuid4()), ServerId(uuid.uuid4())
    sc, ss = ServersCommunityId(c.value), ServersServerId(s.value)

    asyncio.run(_publish(storage, c, s, {"k": b"snap"}, worker))
    base = asyncio.run(storage.current_generation(c, s))

    uow = FakeUnitOfWork()
    uow.servers.seed(_held_server(c, s, worker))
    write = WriteFile(
        uow=uow,
        control_plane=FakeControlPlane(),
        file_store=StorageFileStoreAdapter(storage=storage),
    )

    # The operator edits a file while the final snapshot is still in flight.
    with pytest.raises(ServerAssignedError):
        asyncio.run(
            write(community_id=sc, server_id=ss, rel_path="k", content=b"edited")
        )

    assert asyncio.run(storage.current_generation(c, s)) == base
    assert asyncio.run(storage.current_publisher(c, s)) == worker
    assert asyncio.run(_live(storage, c, s)) == {"k": b"snap"}

    status = _post_snapshot(
        data_plane(storage, worker),
        c,
        s,
        base=base,
        worker=worker,
        files={"k": b"final-snapshot"},
    )
    assert status == 204
    assert asyncio.run(storage.current_generation(c, s)) == base + 1
    assert asyncio.run(storage.current_publisher(c, s)) == worker
    assert asyncio.run(_live(storage, c, s)) == {"k": b"final-snapshot"}


@_BACKENDS
def test_a_server_held_by_a_refused_final_snapshot_keeps_its_world_for_the_retry(
    tmp_path: Path,
    make_storage: Callable[[Path], Storage],
    data_plane: Callable[[Storage, str], TestClient],
) -> None:
    # Issue #3697: the Worker refuses the stop's final snapshot UNAVAILABLE (a
    # leftover container it could not end may still write the working set). The
    # server stays assigned with the refusal recorded, so an edit cannot advance
    # the store under the snapshot still owed. The retry then publishes, and the
    # stop that drives it releases the server.
    worker = str(uuid.uuid4())
    storage = make_storage(tmp_path)
    c, s = CommunityId(uuid.uuid4()), ServerId(uuid.uuid4())
    sc, ss = ServersCommunityId(c.value), ServersServerId(s.value)

    asyncio.run(_publish(storage, c, s, {"k": b"snap"}, worker))
    base = asyncio.run(storage.current_generation(c, s))

    uow = FakeUnitOfWork()
    running = _held_server(c, s, worker)
    running.desired_state = DesiredState.RUNNING
    running.observed_state = ObservedState.RUNNING
    running.observed_at = _NOW - dt.timedelta(minutes=5)
    uow.servers.seed(running)
    refused = FakeControlPlane(
        outcomes={"snapshot": CommandOutcome(status=CommandStatus.UNAVAILABLE)}
    )
    held = asyncio.run(
        StopServer(uow=uow, control_plane=refused, clock=FakeClock(_NOW))(
            community_id=sc, server_id=ss
        )
    )
    assert held.assigned_worker_id == WorkerId(uuid.UUID(worker))
    assert held.final_snapshot_refusal is FinalSnapshotRefusal.UNAVAILABLE

    write = WriteFile(
        uow=uow,
        control_plane=FakeControlPlane(),
        file_store=StorageFileStoreAdapter(storage=storage),
    )
    with pytest.raises(ServerAssignedError):
        asyncio.run(
            write(community_id=sc, server_id=ss, rel_path="k", content=b"edited")
        )
    assert asyncio.run(storage.current_generation(c, s)) == base
    assert asyncio.run(_live(storage, c, s)) == {"k": b"snap"}

    # The retry: the Worker's final snapshot publishes through the data plane,
    # and the snapshot command it answers releases the server.
    status = _post_snapshot(
        data_plane(storage, worker),
        c,
        s,
        base=base,
        worker=worker,
        files={"k": b"final-snapshot"},
    )
    assert status == 204
    released = asyncio.run(
        StopServer(uow=uow, control_plane=FakeControlPlane(), clock=FakeClock(_NOW))(
            community_id=sc, server_id=ss
        )
    )
    assert released.assigned_worker_id is None
    assert released.final_snapshot_refusal is None
    assert uow.servers.by_id[ss].assigned_worker_id is None
    assert asyncio.run(storage.current_generation(c, s)) == base + 1
    assert asyncio.run(storage.current_publisher(c, s)) == worker
    assert asyncio.run(_live(storage, c, s)) == {"k": b"final-snapshot"}


@_BACKENDS
def test_an_adopted_instance_behind_a_start_time_edit_still_publishes(
    tmp_path: Path,
    make_storage: Callable[[Path], Storage],
    data_plane: Callable[[Storage, str], TestClient],
) -> None:
    # The #1703 exemption's live case (#3865). A start writes eula.txt or an owed
    # group file into the store (an ``api-edit`` sentinel publish), then finds
    # its placed Worker already running the server and adopts that instance
    # instead of hydrating it. The instance's scratch lags the sentinel
    # ``current``; without the exemption every snapshot of that run, its final
    # one included, would be refused.
    worker = str(uuid.uuid4())
    storage = make_storage(tmp_path)
    c, s = CommunityId(uuid.uuid4()), ServerId(uuid.uuid4())

    asyncio.run(_publish(storage, c, s, {"k": b"instance"}, worker))
    base = asyncio.run(storage.current_generation(c, s))
    asyncio.run(storage.write_file(c, s, RelPath("eula.txt"), b"eula=true\n"))
    assert asyncio.run(storage.current_publisher(c, s)) == API_EDIT_PUBLISHER
    assert asyncio.run(storage.current_generation(c, s)) == base + 1

    status = _post_snapshot(
        data_plane(storage, worker),
        c,
        s,
        base=base,
        worker=worker,
        files={"k": b"adopted-run"},
    )

    assert status == 204
    assert asyncio.run(storage.current_publisher(c, s)) == worker
    assert asyncio.run(_live(storage, c, s)) == {"k": b"adopted-run"}
