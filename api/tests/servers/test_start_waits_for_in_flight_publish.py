"""A start's generation read waits for a publish already past its fence (#3552).

The race these tests pin runs through the real lifecycle. Worker A's final
snapshot stalls in the API's commit until the Worker's transfer deadline, so A
answers ``TRANSFER_FAILED`` while the commit is still running, and that failed
result releases A's hold on the server. A's commit then reaches the assignment
fence under the publish lock. Nobody is assigned, so the fence passes, and the
commit has yet to publish. A start now places the server on Worker B. B still
holds the scratch of its own earlier run at the store's previous generation.

If the start reads the generation without the publish lock, it reads that
previous generation and skips the hydrate. A's commit then publishes the next
generation, and B runs on the older world. The read has to wait for the publish.

Each test holds A's commit just past the fence until the start has finished, or
for ``_HOLD`` seconds if it cannot finish while the commit holds the lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import io
import tarfile
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from mc_server_dashboard_api.dataplane.api.transfers import _assignment_fence
from mc_server_dashboard_api.servers.adapters.store_generation import (
    StorageGenerationReader,
)
from mc_server_dashboard_api.servers.application.lifecycle import (
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.port import SnapshotHandle, Storage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.servers.fakes import (
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeUnitOfWork,
)
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory

_NOW = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.timezone.utc)
_HOLD = 0.5
_WORKER_A = WorkerId(uuid.uuid4())
_WORKER_B = WorkerId(uuid.uuid4())


def _fs_storage(tmp_path: Path) -> Storage:
    return FsStorage(tmp_path)


def _object_storage(tmp_path: Path) -> Storage:
    del tmp_path
    return ObjectStorage(fake_s3_factory(FakeS3Store()))


_BACKENDS = pytest.mark.parametrize(
    "make_storage", [_fs_storage, _object_storage], ids=["fs", "object"]
)


def _tar(content: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="world/level.dat")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


async def _stage(storage: Storage, server: Server, content: bytes) -> SnapshotHandle:
    handle = await storage.begin_snapshot(
        StorageCommunityId(server.community_id.value),
        StorageServerId(server.id.value),
    )

    async def _stream() -> AsyncIterator[bytes]:
        yield _tar(content)

    await storage.write_snapshot(handle, _stream())
    return handle


def _running_on_a() -> Server:
    return Server(
        id=ServerId.new(),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.RUNNING,
        observed_state=ObservedState.RUNNING,
        observed_at=_NOW,
        assigned_worker_id=_WORKER_A,
        created_at=_NOW,
        updated_at=_NOW,
    )


async def _release_a_with_its_publish_past_the_fence(
    storage: Storage,
    uow: FakeUnitOfWork,
    cp: FakeControlPlane,
    server: Server,
    finished: asyncio.Event,
) -> asyncio.Task[int]:
    """Stop A, fail its final snapshot, and start A's commit up to the publish.

    Returns the commit, held past the fence until ``finished`` is set (or
    ``_HOLD`` lapses).
    """

    # B's earlier run published generation 1; B still holds that scratch.
    first = await _stage(storage, server, b"b-run")
    await storage.commit_snapshot(first, publisher=str(_WORKER_B.value))
    # A's final snapshot has finished uploading; its commit is still to come.
    late = await _stage(storage, server, b"a-run")
    # The failed final snapshot releases A's hold (lifecycle.py, _final_snapshot).
    await StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))(
        community_id=server.community_id, server_id=server.id
    )
    assert uow.servers.by_id[server.id].assigned_worker_id is None

    async def _assigned(_c: uuid.UUID, s: uuid.UUID) -> str | None:
        worker = uow.servers.by_id[ServerId(s)].assigned_worker_id
        return None if worker is None else str(worker.value)

    fence = _assignment_fence(
        _assigned, server.community_id.value, server.id.value, str(_WORKER_A.value)
    )
    passed = asyncio.Event()

    async def _held_past_fence() -> bool:
        allowed = await fence()
        passed.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(finished.wait(), _HOLD)
        return allowed

    commit = asyncio.create_task(
        storage.commit_snapshot(
            late,
            publisher=str(_WORKER_A.value),
            expected_base=1,
            publisher_fence=_held_past_fence,
        )
    )
    await passed.wait()
    return commit


def _start(uow: FakeUnitOfWork, cp: FakeControlPlane, storage: Storage) -> StartServer:
    return StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=StorageGenerationReader(storage=storage),
        file_store=FakeFileStore(seed_eula=True),
    )


def _setup(
    make_storage: Callable[[Path], Storage], tmp_path: Path
) -> tuple[Storage, FakeUnitOfWork, FakeControlPlane, Server]:
    storage = make_storage(tmp_path)
    uow = FakeUnitOfWork()
    server = _running_on_a()
    uow.servers.seed(server)
    failed = CommandOutcome(status=CommandStatus.TRANSFER_FAILED, message="deadline")
    cp = FakeControlPlane(
        place_to=_WORKER_B,
        held={(_WORKER_B, server.id): 1},
        outcomes={"snapshot": failed},
    )
    return storage, uow, cp, server


@_BACKENDS
async def test_start_hydrates_over_a_publish_past_its_fence(
    tmp_path: Path, make_storage: Callable[[Path], Storage]
) -> None:
    storage, uow, cp, server = _setup(make_storage, tmp_path)
    finished = asyncio.Event()
    commit = await _release_a_with_its_publish_past_the_fence(
        storage, uow, cp, server, finished
    )

    try:
        await _start(uow, cp, storage)(
            community_id=server.community_id, server_id=server.id
        )
    finally:
        finished.set()
    assert await commit == 2

    # B holds generation 1 and the store is at 2, so B must hydrate first.
    assert [kind for kind, worker, _ in cp.dispatched if worker == _WORKER_B] == [
        "hydrate",
        "start",
    ]


@_BACKENDS
async def test_redispatch_start_hydrates_over_a_publish_past_its_fence(
    tmp_path: Path, make_storage: Callable[[Path], Storage]
) -> None:
    storage, uow, cp, server = _setup(make_storage, tmp_path)
    finished = asyncio.Event()
    commit = await _release_a_with_its_publish_past_the_fence(
        storage, uow, cp, server, finished
    )
    # A start committed the placement on B, then was cancelled before its launch,
    # so the reconciler's redispatch_start is what dispatches it.
    row = uow.servers.by_id[server.id]
    row.desired_state = DesiredState.RUNNING
    row.assigned_worker_id = _WORKER_B

    try:
        await _start(uow, cp, storage).redispatch_start(
            community_id=server.community_id, server_id=server.id
        )
    finally:
        finished.set()
    assert await commit == 2

    assert [kind for kind, worker, _ in cp.dispatched if worker == _WORKER_B] == [
        "hydrate",
        "start",
    ]
