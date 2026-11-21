"""A start waits for its Worker's uploads before it hydrates it (issue #3950).

The same-publisher lag allowance (issue #847 bug 3) accepts a publish whose base
is older than ``current`` when the same Worker published ``current``: its
acknowledgement was lost, so the Worker's marker stayed behind while its working
set went on from the tree it published.

A Worker's command can end before the API's commit of its upload does: the
Worker's transfer deadline or a Worker restart ends the request while the API
keeps the body it already has. A start that hydrated the Worker before that commit
would run it on the older generation, and the commit would then publish a world
the run does not contain. So a start that hydrates a Worker first waits for that
Worker's uploads of the server: the commit lands, and the hydrate serves it.

These tests drive that through the real data plane, storage and lifecycle. Worker
A's upload is staged, and its commit is held until the start has begun to wait for
it or has finished.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import tarfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient, Response
from starlette.requests import ClientDisconnect

from mc_server_dashboard_api.dataplane.api.transfers import hydrate_working_set
from mc_server_dashboard_api.dataplane.api.uploads import SnapshotUploads
from mc_server_dashboard_api.dependencies import (
    WorkerAssignment,
    get_assigned_worker_lookup,
    get_resolved_jar_lookup,
    get_snapshot_uploads,
    get_storage,
    get_transfer_semaphore,
    get_worker_credential,
)
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
from mc_server_dashboard_api.servers.domain.errors import (
    CommandDispatchError,
    SnapshotUploadInFlightError,
)
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
from mc_server_dashboard_api.storage.domain.port import (
    API_EDIT_PUBLISHER,
    RESTORE_PUBLISHER,
    SnapshotHandle,
    Storage,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import RelPath
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.servers.fakes import (
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeLifecycleLock,
    FakeUnitOfWork,
)
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory

_CREDENTIAL = "test-worker-credential"
_NOW = dt.datetime(2026, 11, 21, 12, 0, tzinfo=dt.timezone.utc)
_WORKER_A = WorkerId(uuid.uuid4())
_WORKER_B = WorkerId(uuid.uuid4())
_DEADLINE = CommandOutcome(status=CommandStatus.TRANSFER_FAILED, message="deadline")
# Far past a commit's fsyncs on a loaded host, so only a wait that never ends runs
# it out.
_WAIT_SECONDS = 30.0


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


def _untar(blob: bytes) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as tar:
        member = tar.extractfile("world/level.dat")
        assert member is not None
        return member.read()


def _scope(server: Server) -> tuple[StorageCommunityId, StorageServerId]:
    return StorageCommunityId(server.community_id.value), StorageServerId(
        server.id.value
    )


async def _publish(storage: Storage, server: Server, content: bytes) -> int:
    handle = await storage.begin_snapshot(*_scope(server))

    async def _stream() -> AsyncIterator[bytes]:
        yield _tar(content)

    await storage.write_snapshot(handle, _stream())
    return await storage.commit_snapshot(handle, publisher=str(_WORKER_A.value))


async def _live(storage: Storage, server: Server) -> tuple[int, bytes]:
    """The store's generation and the world it holds."""

    c, s = _scope(server)
    blob = b"".join([chunk async for chunk in storage.open_hydrate_source(c, s)])
    return await storage.current_generation(c, s), _untar(blob)


async def _no_jar(_c: uuid.UUID, _s: uuid.UUID) -> str | None:
    return None


async def _serve_hydrate(
    storage: Storage, community: uuid.UUID, server: uuid.UUID
) -> int:
    """Serve a hydrate through the data plane's handler, as a Worker's GET does.

    Returns the generation it served.
    """

    response = await hydrate_working_set(
        community, server, storage, _no_jar, asyncio.Semaphore(1)
    )
    async for _ in response.body_iterator:
        pass
    return int(response.headers["X-Working-Set-Generation"])


class _WorkerServesHydrates(FakeControlPlane):
    """A control plane whose Workers pull each hydrate from the real store.

    The pull is served whatever the hydrate then answers, as a Worker whose
    unpack fails after the stream has been read.
    """

    def __init__(
        self,
        storage: Storage,
        *,
        held: dict[tuple[WorkerId, ServerId], int],
        place_to: WorkerId | None = None,
        outcomes: dict[str, CommandOutcome] | None = None,
    ) -> None:
        super().__init__(place_to=place_to, held=held, outcomes=outcomes)
        self._storage = storage
        self.served: list[int] = []

    async def hydrate(
        self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
    ) -> CommandOutcome:
        outcome = await super().hydrate(
            worker_id=worker_id, community_id=community_id, server_id=server_id
        )
        self.served.append(
            await _serve_hydrate(self._storage, community_id.value, server_id.value)
        )
        return outcome


class _WatchedUploads(SnapshotUploads):
    """The uploads in flight, recording each wait that finds one to wait for.

    It records whether the start held its lifecycle lock as it began to wait.
    """

    def __init__(self) -> None:
        super().__init__()
        self.lock = FakeLifecycleLock()
        self.waiting = asyncio.Event()
        self.held_at_wait: list[bool] = []

    async def settled(
        self, server_id: uuid.UUID, worker_id: str, *, timeout: float
    ) -> bool:
        if await super().settled(server_id, worker_id, timeout=0):
            return True
        kinds = [kind for _, kind in self.lock.events]
        self.held_at_wait.append(kinds.count("acquire") > kinds.count("release"))
        self.waiting.set()
        return await super().settled(server_id, worker_id, timeout=timeout)


def _server(*, desired: DesiredState, observed: ObservedState, on_a: bool) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=desired,
        observed_state=observed,
        observed_at=_NOW - dt.timedelta(minutes=1),
        assigned_worker_id=_WORKER_A if on_a else None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _start(
    uow: FakeUnitOfWork,
    cp: FakeControlPlane,
    storage: Storage,
    uploads: _WatchedUploads,
    *,
    wait_seconds: float = _WAIT_SECONDS,
) -> StartServer:
    return StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=StorageGenerationReader(
            storage=storage, uploads=uploads, upload_wait_seconds=wait_seconds
        ),
        file_store=FakeFileStore(seed_eula=True),
        lifecycle_lock=uploads.lock,
    )


async def _stop(uow: FakeUnitOfWork, cp: FakeControlPlane, server: Server) -> None:
    """Stop the server; its final snapshot answers at A's deadline, releasing A.

    The stop runs before the start, so its writes are stamped earlier.
    """

    stopped_at = _NOW - dt.timedelta(seconds=30)
    await StopServer(uow=uow, control_plane=cp, clock=FakeClock(stopped_at))(
        community_id=server.community_id, server_id=server.id
    )
    assert uow.servers.by_id[server.id].assigned_worker_id is None


async def _nothing() -> None:
    return None


def _bind(
    app: FastAPI, storage: Storage, uow: FakeUnitOfWork, uploads: SnapshotUploads
) -> None:
    """Serve the data plane from ``storage``, reading assignments from ``uow``."""

    app.dependency_overrides.clear()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_resolved_jar_lookup] = lambda: _no_jar

    async def _assigned(_c: uuid.UUID, s: uuid.UUID) -> WorkerAssignment | None:
        row = uow.servers.by_id[ServerId(s)]
        if row.assigned_worker_id is None:
            return None
        return WorkerAssignment(
            worker_id=str(row.assigned_worker_id.value),
            awaiting_hydrate=row.awaiting_hydrate,
        )

    app.dependency_overrides[get_assigned_worker_lookup] = lambda: _assigned
    app.dependency_overrides[get_worker_credential] = lambda: _CREDENTIAL
    semaphore = asyncio.Semaphore(4)
    app.dependency_overrides[get_transfer_semaphore] = lambda: semaphore
    app.dependency_overrides[get_snapshot_uploads] = lambda: uploads


async def _upload_meets_start(
    app: FastAPI,
    storage: Storage,
    uow: FakeUnitOfWork,
    uploads: _WatchedUploads,
    server: Server,
    *,
    base: int | None,
    start: Callable[[], Coroutine[Any, Any, object]],
    before_start: Callable[[], Awaitable[None]] = _nothing,
    release_on_wait: bool = True,
    give_up: bool = False,
) -> tuple[Response, BaseException | None]:
    """Run Worker A's upload into ``start``; return A's answer and what it raised.

    A's body is staged, and then ``before_start`` and ``start`` run. A's commit is
    held until the start has begun to wait for it (unless ``release_on_wait`` is
    off) or has finished. With ``give_up``, A goes away there instead of
    committing, as a Worker whose request ends mid-body.
    """

    _bind(app, storage, uow, uploads)
    staged = asyncio.Event()
    release = asyncio.Event()
    real_write_snapshot = storage.write_snapshot

    async def _write_snapshot(
        handle: SnapshotHandle,
        stream: AsyncIterator[bytes],
        *,
        expected_base: int | None = None,
    ) -> None:
        await real_write_snapshot(handle, stream, expected_base=expected_base)
        staged.set()
        await release.wait()
        if give_up:
            raise ClientDisconnect()

    storage.write_snapshot = _write_snapshot  # type: ignore[method-assign]
    headers = {
        "Authorization": f"Bearer {_CREDENTIAL}",
        "X-Worker-Id": str(_WORKER_A.value),
    }
    if base is not None:
        headers["X-Working-Set-Base-Generation"] = str(base)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            upload = asyncio.create_task(
                client.post(
                    f"/api/data-plane/communities/{server.community_id.value}"
                    f"/servers/{server.id.value}/snapshot",
                    content=_tar(b"packed-by-a"),
                    headers=headers,
                )
            )
            await staged.wait()
            await before_start()
            started: asyncio.Task[object] = asyncio.create_task(start())
            gates: set[asyncio.Task[Any]] = {started}
            if release_on_wait:
                gates.add(asyncio.create_task(uploads.waiting.wait()))
            await asyncio.wait(gates, return_when=asyncio.FIRST_COMPLETED)
            for gate in gates - {started}:
                gate.cancel()
            release.set()
            resp = await upload
            try:
                await started
            except Exception as exc:
                return resp, exc
            return resp, None
    finally:
        storage.write_snapshot = real_write_snapshot  # type: ignore[method-assign]


def _kinds(cp: FakeControlPlane) -> list[str]:
    return [kind for kind, _, _ in cp.dispatched]


# 14 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(14 * 10 + 30)
@_BACKENDS
def test_a_start_on_the_same_worker_hydrates_the_late_final_snapshot_it_waited_for(
    tmp_path: Path, shared_app: FastAPI, make_storage: Callable[[Path], Storage]
) -> None:
    # A published generations 1 and 2, and the acknowledgement of 2 was lost, so
    # A's marker reads 1 and its working set is the tree it published 2 from. Its
    # final snapshot declares base 1 (the lost-response allowance passes it). The
    # snapshot answers at A's deadline while the API still commits it, and that
    # answer releases A. A start places the server on A again: A holds 1, the
    # store is at 2, so A hydrates. The start waits for the late commit, and the
    # hydrate serves it.
    storage = make_storage(tmp_path)
    server = _server(
        desired=DesiredState.RUNNING, observed=ObservedState.RUNNING, on_a=True
    )
    asyncio.run(_publish(storage, server, b"a-g1"))
    asyncio.run(_publish(storage, server, b"a-g2"))
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = _WorkerServesHydrates(
        storage,
        place_to=_WORKER_A,
        held={(_WORKER_A, server.id): 1},
        outcomes={"snapshot": _DEADLINE},
    )
    uploads = _WatchedUploads()

    resp, error = asyncio.run(
        _upload_meets_start(
            shared_app,
            storage,
            uow,
            uploads,
            server,
            base=1,
            before_start=lambda: _stop(uow, cp, server),
            start=lambda: _start(uow, cp, storage, uploads)(
                community_id=server.community_id, server_id=server.id
            ),
        )
    )

    assert error is None
    assert resp.status_code == 204
    assert _kinds(cp) == ["stop", "snapshot", "hydrate", "start"]
    assert cp.served == [3]
    assert uploads.held_at_wait == [False]
    assert asyncio.run(_live(storage, server)) == (3, b"packed-by-a")


# 12 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(12 * 10 + 30)
@_BACKENDS
def test_a_rerun_after_a_worker_restart_hydrates_the_snapshot_it_waited_for(
    tmp_path: Path, shared_app: FastAPI, make_storage: Callable[[Path], Storage]
) -> None:
    # A runs the server at generation 1. Its periodic snapshot's body has reached
    # the API when A's process dies; the API keeps committing. A restarts, its
    # boot fsck finds the killed world torn and rewrites the marker to 0, and the
    # server keeps its assignment. The reconciler re-sends the start to A, which
    # holds 0, so A hydrates: after the commit, and so the world it publishes.
    storage = make_storage(tmp_path)
    server = _server(
        desired=DesiredState.RUNNING, observed=ObservedState.UNKNOWN, on_a=True
    )
    asyncio.run(_publish(storage, server, b"a-g1"))
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = _WorkerServesHydrates(storage, held={(_WORKER_A, server.id): 0})
    uploads = _WatchedUploads()

    resp, error = asyncio.run(
        _upload_meets_start(
            shared_app,
            storage,
            uow,
            uploads,
            server,
            base=1,
            start=lambda: _start(uow, cp, storage, uploads).redispatch_start(
                community_id=server.community_id, server_id=server.id
            ),
        )
    )

    assert error is None
    assert resp.status_code == 204
    assert _kinds(cp) == ["hydrate", "start"]
    assert cp.served == [2]
    assert uploads.held_at_wait == [False]
    assert asyncio.run(_live(storage, server)) == (2, b"packed-by-a")


# 16 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(16 * 10 + 30)
@_BACKENDS
def test_a_start_that_skips_the_hydrate_does_not_wait_and_the_late_commit_lands(
    tmp_path: Path, shared_app: FastAPI, make_storage: Callable[[Path], Storage]
) -> None:
    # The control: the same late final snapshot, but A holds the store's
    # generation, so the start on A skips the hydrate and A runs on the tree that
    # snapshot packed. Nothing is hydrated over it, so the start does not wait,
    # and the commit lands after it. The next snapshot of A's run, whose base
    # still lags, is the lost-response allowance's case and lands too.
    storage = make_storage(tmp_path)
    server = _server(
        desired=DesiredState.RUNNING, observed=ObservedState.RUNNING, on_a=True
    )
    asyncio.run(_publish(storage, server, b"a-g1"))
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = _WorkerServesHydrates(
        storage,
        place_to=_WORKER_A,
        held={(_WORKER_A, server.id): 1},
        outcomes={"snapshot": _DEADLINE},
    )
    uploads = _WatchedUploads()

    resp, error = asyncio.run(
        _upload_meets_start(
            shared_app,
            storage,
            uow,
            uploads,
            server,
            base=1,
            before_start=lambda: _stop(uow, cp, server),
            start=lambda: _start(uow, cp, storage, uploads)(
                community_id=server.community_id, server_id=server.id
            ),
        )
    )

    assert error is None
    assert resp.status_code == 204
    assert _kinds(cp) == ["stop", "snapshot", "start"]
    assert uploads.held_at_wait == []
    assert asyncio.run(_live(storage, server)) == (2, b"packed-by-a")

    resp, error = asyncio.run(
        _upload_meets_start(
            shared_app, storage, uow, uploads, server, base=1, start=_nothing
        )
    )
    assert (resp.status_code, error) == (204, None)
    assert asyncio.run(_live(storage, server))[0] == 3


# 14 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(14 * 10 + 30)
@_BACKENDS
def test_a_hydrate_that_fails_after_the_wait_leaves_the_late_commit_landed(
    tmp_path: Path, shared_app: FastAPI, make_storage: Callable[[Path], Storage]
) -> None:
    # Review probe A: the first test's start, but A's hydrate fails once the
    # pull was served, leaving A's tree as it was. The start compensates. The
    # late commit landed before the hydrate, so the store holds what A packed.
    storage = make_storage(tmp_path)
    server = _server(
        desired=DesiredState.RUNNING, observed=ObservedState.RUNNING, on_a=True
    )
    asyncio.run(_publish(storage, server, b"a-g1"))
    asyncio.run(_publish(storage, server, b"a-g2"))
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = _WorkerServesHydrates(
        storage,
        place_to=_WORKER_A,
        held={(_WORKER_A, server.id): 1},
        outcomes={"snapshot": _DEADLINE, "hydrate": _DEADLINE},
    )
    uploads = _WatchedUploads()

    resp, error = asyncio.run(
        _upload_meets_start(
            shared_app,
            storage,
            uow,
            uploads,
            server,
            base=1,
            before_start=lambda: _stop(uow, cp, server),
            start=lambda: _start(uow, cp, storage, uploads)(
                community_id=server.community_id, server_id=server.id
            ),
        )
    )

    assert isinstance(error, CommandDispatchError)
    assert resp.status_code == 204
    assert _kinds(cp) == ["stop", "snapshot", "hydrate"]
    assert cp.served == [3]
    row = uow.servers.by_id[server.id]
    assert (row.desired_state, row.assigned_worker_id) == (DesiredState.STOPPED, None)
    assert asyncio.run(_live(storage, server)) == (3, b"packed-by-a")


# 12 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(12 * 10 + 30)
@_BACKENDS
def test_a_start_on_another_worker_does_not_wait_and_the_late_commit_lands_after_it(
    tmp_path: Path, shared_app: FastAPI, make_storage: Callable[[Path], Storage]
) -> None:
    # Review probe B: A's final snapshot answers at A's deadline while the API
    # commits it, and the start is placed on B. B's start does not wait for A's
    # upload: the commit-time fence refuses it while B is assigned. B's hydrate is
    # served and then fails, the start compensates, and A's commit then lands, as
    # it does without the wait.
    storage = make_storage(tmp_path)
    server = _server(
        desired=DesiredState.RUNNING, observed=ObservedState.RUNNING, on_a=True
    )
    asyncio.run(_publish(storage, server, b"a-g1"))
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = _WorkerServesHydrates(
        storage,
        place_to=_WORKER_B,
        held={(_WORKER_A, server.id): 1},
        outcomes={"snapshot": _DEADLINE, "hydrate": _DEADLINE},
    )
    uploads = _WatchedUploads()

    resp, error = asyncio.run(
        _upload_meets_start(
            shared_app,
            storage,
            uow,
            uploads,
            server,
            base=1,
            before_start=lambda: _stop(uow, cp, server),
            start=lambda: _start(uow, cp, storage, uploads)(
                community_id=server.community_id, server_id=server.id
            ),
        )
    )

    assert isinstance(error, CommandDispatchError)
    assert [(kind, worker) for kind, worker, _ in cp.dispatched] == [
        ("stop", _WORKER_A),
        ("snapshot", _WORKER_A),
        ("hydrate", _WORKER_B),
    ]
    assert cp.served == [1]
    assert uploads.held_at_wait == []
    assert resp.status_code == 204
    assert uow.servers.by_id[server.id].assigned_worker_id is None
    assert asyncio.run(_live(storage, server)) == (2, b"packed-by-a")


# 16 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(16 * 10 + 30)
@_BACKENDS
def test_a_start_whose_wait_runs_out_keeps_its_worker_and_is_retried_there(
    tmp_path: Path, shared_app: FastAPI, make_storage: Callable[[Path], Storage]
) -> None:
    # The first test's start, with A's commit held past the wait's bound. The
    # start sends no hydrate and keeps the server on A, observed unknown, which
    # the reconciler retries. Once the commit has landed, that retry hydrates A
    # with it, without placing the server again.
    storage = make_storage(tmp_path)
    server = _server(
        desired=DesiredState.RUNNING, observed=ObservedState.RUNNING, on_a=True
    )
    asyncio.run(_publish(storage, server, b"a-g1"))
    asyncio.run(_publish(storage, server, b"a-g2"))
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = _WorkerServesHydrates(
        storage,
        place_to=_WORKER_A,
        held={(_WORKER_A, server.id): 1},
        outcomes={"snapshot": _DEADLINE},
    )
    uploads = _WatchedUploads()

    resp, error = asyncio.run(
        _upload_meets_start(
            shared_app,
            storage,
            uow,
            uploads,
            server,
            base=1,
            before_start=lambda: _stop(uow, cp, server),
            start=lambda: _start(uow, cp, storage, uploads, wait_seconds=0.05)(
                community_id=server.community_id, server_id=server.id
            ),
            release_on_wait=False,
        )
    )

    assert isinstance(error, SnapshotUploadInFlightError)
    assert _kinds(cp) == ["stop", "snapshot"]
    assert cp.served == []
    row = uow.servers.by_id[server.id]
    assert (row.desired_state, row.assigned_worker_id, row.observed_state) == (
        DesiredState.RUNNING,
        _WORKER_A,
        ObservedState.UNKNOWN,
    )
    assert resp.status_code == 204
    assert asyncio.run(_live(storage, server)) == (3, b"packed-by-a")

    asyncio.run(
        _start(uow, cp, storage, uploads).redispatch_start(
            community_id=server.community_id, server_id=server.id
        )
    )

    assert _kinds(cp) == ["stop", "snapshot", "hydrate", "start"]
    assert cp.served == [3]
    assert cp.reserved == [(_WORKER_A, server.id)]


# 12 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(12 * 10 + 30)
@_BACKENDS
def test_an_upload_that_gives_up_releases_the_start_waiting_for_it(
    tmp_path: Path, shared_app: FastAPI, make_storage: Callable[[Path], Storage]
) -> None:
    # The first test's start, but A's request ends before its body is whole, so
    # the upload aborts. The start waiting for it goes on at once and hydrates
    # what the store still holds.
    storage = make_storage(tmp_path)
    server = _server(
        desired=DesiredState.RUNNING, observed=ObservedState.RUNNING, on_a=True
    )
    asyncio.run(_publish(storage, server, b"a-g1"))
    asyncio.run(_publish(storage, server, b"a-g2"))
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = _WorkerServesHydrates(
        storage,
        place_to=_WORKER_A,
        held={(_WORKER_A, server.id): 1},
        outcomes={"snapshot": _DEADLINE},
    )
    uploads = _WatchedUploads()

    resp, error = asyncio.run(
        _upload_meets_start(
            shared_app,
            storage,
            uow,
            uploads,
            server,
            base=1,
            before_start=lambda: _stop(uow, cp, server),
            start=lambda: _start(uow, cp, storage, uploads)(
                community_id=server.community_id, server_id=server.id
            ),
            give_up=True,
        )
    )

    assert error is None
    assert resp.status_code == 400
    assert _kinds(cp) == ["stop", "snapshot", "hydrate", "start"]
    assert uploads.held_at_wait == [False]
    assert cp.served == [2]
    assert asyncio.run(_live(storage, server)) == (2, b"a-g2")


async def _restore(storage: Storage, server: Server) -> None:
    c, s = _scope(server)
    key = await storage.create_backup_from_current(c, s)
    await _publish(storage, server, b"a-g2")
    await storage.restore_backup(c, s, key)


async def _edit(storage: Storage, server: Server) -> None:
    c, s = _scope(server)
    await _publish(storage, server, b"a-g2")
    await storage.write_file(c, s, RelPath("world/level.dat"), b"a-g1")


# 24 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(24 * 10 + 30)
@pytest.mark.parametrize("bump", [_restore, _edit], ids=["restore", "edit"])
@_BACKENDS
def test_a_start_after_a_restore_or_edit_hydrates_instead_of_skipping(
    tmp_path: Path,
    make_storage: Callable[[Path], Storage],
    bump: Callable[[Storage, Server], Coroutine[Any, Any, None]],
) -> None:
    # Issue #3950's restore-then-skip-hydrate sequence. A ran the server and holds
    # the generation it last published, 2. An operator restores generation 1's
    # world, or edits the stopped server back to it. That bump stamps an API
    # publisher, so no publish of A's can be the same-publisher lag, and it moves
    # the generation past what A holds, so the next start on A hydrates the bumped
    # world rather than skipping onto A's older scratch.
    storage = make_storage(tmp_path)
    server = _server(
        desired=DesiredState.STOPPED, observed=ObservedState.STOPPED, on_a=False
    )
    asyncio.run(_publish(storage, server, b"a-g1"))
    asyncio.run(bump(storage, server))
    c, s = _scope(server)
    assert asyncio.run(storage.current_publisher(c, s)) in {
        RESTORE_PUBLISHER,
        API_EDIT_PUBLISHER,
    }
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = _WorkerServesHydrates(
        storage, place_to=_WORKER_A, held={(_WORKER_A, server.id): 2}
    )

    asyncio.run(
        _start(uow, cp, storage, _WatchedUploads())(
            community_id=server.community_id, server_id=server.id
        )
    )

    assert _kinds(cp) == ["hydrate", "start"]
    assert cp.served == [3]
    assert asyncio.run(_live(storage, server)) == (3, b"a-g1")
