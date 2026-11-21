"""An upload packed before a hydrate of its server does not land (issue #3950).

The same-publisher lag allowance (issue #847 bug 3) accepts a publish whose base
is older than ``current`` when the same Worker published ``current``: its
acknowledgement was lost, so the Worker's marker stayed behind while its working
set went on from the tree it published. That holds only while the Worker's
working set is still that tree, and a hydrate is the one thing that replaces it.

A Worker's command can end before the API's commit of its upload does: the
Worker's transfer deadline, a dropped stream, or a Worker restart ends the
request while the API keeps the body it already has. If a start then hydrates
the Worker before that commit's flip, the Worker gets the older generation and
runs on it, while the commit then publishes the generation it does not contain.
These tests drive that through the real data plane, storage and lifecycle. The
commit is refused instead (``409 hydrated_during_upload``), and a commit that no
hydrate overtook still lands.
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
from fastapi.testclient import TestClient
from httpx2 import Response

from mc_server_dashboard_api.dataplane.api.transfers import hydrate_working_set
from mc_server_dashboard_api.dependencies import (
    WorkerAssignment,
    get_assigned_worker_lookup,
    get_resolved_jar_lookup,
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
    FakeUnitOfWork,
)
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory

_CREDENTIAL = "test-worker-credential"
_NOW = dt.datetime(2026, 11, 21, 12, 0, tzinfo=dt.timezone.utc)
_WORKER_A = WorkerId(uuid.uuid4())
_DEADLINE = CommandOutcome(status=CommandStatus.TRANSFER_FAILED, message="deadline")


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
    """A control plane whose Worker pulls each hydrate from the real store."""

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
        observed_at=_NOW,
        assigned_worker_id=_WORKER_A if on_a else None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _start(uow: FakeUnitOfWork, cp: FakeControlPlane, storage: Storage) -> StartServer:
    return StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=StorageGenerationReader(storage=storage),
        file_store=FakeFileStore(seed_eula=True),
    )


def _post_from_a(
    app: FastAPI,
    storage: Storage,
    uow: FakeUnitOfWork,
    server: Server,
    *,
    base: int | None,
    during_upload: Callable[[], Awaitable[None]],
) -> Response:
    """POST Worker A's snapshot, running ``during_upload`` once its body is staged.

    That is after the guard and the upload stream and before the commit: the
    window in which A's command can end while the API still holds the body. The
    data plane reads the assignment from ``uow``, as the real lookup reads it.
    """

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
    semaphore = asyncio.Semaphore(1)
    app.dependency_overrides[get_transfer_semaphore] = lambda: semaphore

    real_write_snapshot = storage.write_snapshot

    async def _write_snapshot(
        handle: SnapshotHandle,
        stream: AsyncIterator[bytes],
        *,
        expected_base: int | None = None,
    ) -> None:
        await real_write_snapshot(handle, stream, expected_base=expected_base)
        await during_upload()

    storage.write_snapshot = _write_snapshot  # type: ignore[method-assign]
    headers = {
        "Authorization": f"Bearer {_CREDENTIAL}",
        "X-Worker-Id": str(_WORKER_A.value),
    }
    if base is not None:
        headers["X-Working-Set-Base-Generation"] = str(base)
    try:
        with TestClient(app) as client:
            return client.post(
                f"/api/data-plane/communities/{server.community_id.value}"
                f"/servers/{server.id.value}/snapshot",
                content=_tar(b"packed-by-a"),
                headers=headers,
            )
    finally:
        storage.write_snapshot = real_write_snapshot  # type: ignore[method-assign]


# 14 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(14 * 10 + 30)
@_BACKENDS
def test_a_late_final_snapshot_is_refused_once_a_new_placement_hydrated_its_worker(
    tmp_path: Path, shared_app: FastAPI, make_storage: Callable[[Path], Storage]
) -> None:
    # A published generations 1 and 2, and the acknowledgement of 2 was lost, so
    # A's marker reads 1 and its working set is the tree it published 2 from. Its
    # final snapshot declares base 1 (the lost-response allowance passes it). The
    # snapshot answers at A's deadline while the API still commits it, and that
    # answer releases A. A start places the server on A again: A holds 1, the
    # store is at 2, so A hydrates 2 and runs on it. The late commit would publish
    # a world A's new run does not contain.
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

    async def _stop_then_start_on_a() -> None:
        await StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))(
            community_id=server.community_id, server_id=server.id
        )
        assert uow.servers.by_id[server.id].assigned_worker_id is None
        await _start(uow, cp, storage)(
            community_id=server.community_id, server_id=server.id
        )

    resp = _post_from_a(
        shared_app, storage, uow, server, base=1, during_upload=_stop_then_start_on_a
    )

    assert [kind for kind, _, _ in cp.dispatched] == [
        "stop",
        "snapshot",
        "hydrate",
        "start",
    ]
    assert cp.served == [2]
    assert resp.status_code == 409
    assert resp.json()["reason"] == "hydrated_during_upload"
    assert asyncio.run(_live(storage, server)) == (2, b"a-g2")


# 12 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(12 * 10 + 30)
@_BACKENDS
def test_a_snapshot_from_before_a_worker_restart_is_refused_once_its_rerun_hydrated(
    tmp_path: Path, shared_app: FastAPI, make_storage: Callable[[Path], Storage]
) -> None:
    # A runs the server at generation 1. Its periodic snapshot's body has reached
    # the API when A's process dies; the API keeps committing. A restarts, its
    # boot fsck finds the killed world torn and rewrites the marker to 0, and the
    # server keeps its assignment. The reconciler re-sends the start to A, which
    # holds 0, so A hydrates 1 and runs on it.
    storage = make_storage(tmp_path)
    server = _server(
        desired=DesiredState.RUNNING, observed=ObservedState.UNKNOWN, on_a=True
    )
    asyncio.run(_publish(storage, server, b"a-g1"))
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    cp = _WorkerServesHydrates(storage, held={(_WORKER_A, server.id): 0})

    async def _rerun_on_a() -> None:
        await _start(uow, cp, storage).redispatch_start(
            community_id=server.community_id, server_id=server.id
        )

    resp = _post_from_a(
        shared_app, storage, uow, server, base=1, during_upload=_rerun_on_a
    )

    assert [kind for kind, _, _ in cp.dispatched] == ["hydrate", "start"]
    assert cp.served == [1]
    assert resp.status_code == 409
    assert resp.json()["reason"] == "hydrated_during_upload"
    assert asyncio.run(_live(storage, server)) == (1, b"a-g1")


# 16 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(16 * 10 + 30)
@_BACKENDS
def test_a_late_final_snapshot_lands_when_the_new_placement_skipped_the_hydrate(
    tmp_path: Path, shared_app: FastAPI, make_storage: Callable[[Path], Storage]
) -> None:
    # The control: the same late final snapshot, but A holds the store's
    # generation, so the start on A skips the hydrate and A runs on the tree that
    # snapshot packed. The commit lands, and the next snapshot of A's run, whose
    # base still lags, is the lost-response allowance's case and lands too.
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

    async def _stop_then_start_on_a() -> None:
        await StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))(
            community_id=server.community_id, server_id=server.id
        )
        await _start(uow, cp, storage)(
            community_id=server.community_id, server_id=server.id
        )

    resp = _post_from_a(
        shared_app, storage, uow, server, base=1, during_upload=_stop_then_start_on_a
    )

    assert [kind for kind, _, _ in cp.dispatched] == ["stop", "snapshot", "start"]
    assert resp.status_code == 204
    assert asyncio.run(_live(storage, server)) == (2, b"packed-by-a")

    async def _nothing() -> None:
        return None

    resp = _post_from_a(
        shared_app, storage, uow, server, base=1, during_upload=_nothing
    )
    assert resp.status_code == 204
    assert asyncio.run(_live(storage, server))[0] == 3


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
        _start(uow, cp, storage)(community_id=server.community_id, server_id=server.id)
    )

    assert [kind for kind, _, _ in cp.dispatched] == ["hydrate", "start"]
    assert cp.served == [3]
    assert asyncio.run(_live(storage, server)) == (3, b"a-g1")


# 14 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(14 * 10 + 30)
@pytest.mark.parametrize("published", [True, False], ids=["published", "unpublished"])
@_BACKENDS
def test_an_upload_is_refused_once_its_server_was_hydrated_during_it(
    tmp_path: Path,
    shared_app: FastAPI,
    make_storage: Callable[[Path], Storage],
    published: bool,
) -> None:
    # The data plane alone: a hydrate served between an upload's guard and its
    # commit refuses the commit, whatever the hydrate found. One that found
    # nothing published (a 204, or a JAR-only tar) replaced the Worker's working
    # set as much as one that served a world.
    storage = make_storage(tmp_path)
    server = _server(
        desired=DesiredState.RUNNING, observed=ObservedState.RUNNING, on_a=True
    )
    if published:
        asyncio.run(_publish(storage, server, b"a-g1"))
    uow = FakeUnitOfWork()
    uow.servers.seed(server)
    served: list[int] = []

    async def _hydrate() -> None:
        served.append(
            await _serve_hydrate(storage, server.community_id.value, server.id.value)
        )

    resp = _post_from_a(
        shared_app,
        storage,
        uow,
        server,
        base=1 if published else None,
        during_upload=_hydrate,
    )

    assert served == [1 if published else 0]
    assert resp.status_code == 409
    assert resp.json()["reason"] == "hydrated_during_upload"
    c, s = _scope(server)
    assert asyncio.run(storage.current_generation(c, s)) == (1 if published else 0)
