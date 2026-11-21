"""The data plane's count of the snapshot uploads in flight (issue #3950).

A start that hydrates a Worker waits in it for that Worker's uploads of the server
(``tests/dataplane/test_start_waits_for_worker_uploads.py``). These tests pin the
count itself, when the publish handler enters and leaves it, and how long an
upload whose Worker has gone away stays in it, on the production HTTP server.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import tarfile
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

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
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.port import SnapshotHandle, Storage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    ServerId,
)
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory

_CREDENTIAL = "test-worker-credential"
_SERVER = uuid.uuid4()
_WORKER_A = str(uuid.uuid4())
_WORKER_B = str(uuid.uuid4())


async def _turns(n: int = 5) -> None:
    """Let the other tasks run until each is blocked again."""

    for _ in range(n):
        await asyncio.sleep(0)


async def test_a_wait_with_nothing_in_flight_returns_at_once() -> None:
    uploads = SnapshotUploads()

    assert await uploads.settled(_SERVER, _WORKER_A, timeout=0)


async def test_a_wait_returns_once_the_upload_it_waits_for_leaves() -> None:
    uploads = SnapshotUploads()
    leave = asyncio.Event()

    async def _upload() -> None:
        async with uploads.track(_SERVER, _WORKER_A):
            await leave.wait()

    upload = asyncio.create_task(_upload())
    await _turns()
    wait = asyncio.create_task(uploads.settled(_SERVER, _WORKER_A, timeout=30))
    await _turns()
    assert not wait.done()

    leave.set()

    assert await wait
    await upload


async def test_a_wait_outlasts_every_upload_of_its_worker() -> None:
    uploads = SnapshotUploads()
    first, second = asyncio.Event(), asyncio.Event()

    async def _upload(leave: asyncio.Event) -> None:
        async with uploads.track(_SERVER, _WORKER_A):
            await leave.wait()

    tasks = [asyncio.create_task(_upload(first)), asyncio.create_task(_upload(second))]
    await _turns()
    wait = asyncio.create_task(uploads.settled(_SERVER, _WORKER_A, timeout=30))
    first.set()
    await _turns()
    assert not wait.done()

    second.set()

    assert await wait
    await asyncio.gather(*tasks)


async def test_a_wait_does_not_wait_for_another_workers_or_servers_uploads() -> None:
    uploads = SnapshotUploads()

    async with (
        uploads.track(_SERVER, _WORKER_B),
        uploads.track(uuid.uuid4(), _WORKER_A),
    ):
        assert await uploads.settled(_SERVER, _WORKER_A, timeout=0)


async def test_an_upload_naming_no_worker_is_not_counted() -> None:
    uploads = SnapshotUploads()

    async with uploads.track(_SERVER, None):
        assert await uploads.settled(_SERVER, _WORKER_A, timeout=0)


async def test_a_wait_that_runs_out_answers_false() -> None:
    uploads = SnapshotUploads()

    async with uploads.track(_SERVER, _WORKER_A):
        assert not await uploads.settled(_SERVER, _WORKER_A, timeout=0.01)

    assert await uploads.settled(_SERVER, _WORKER_A, timeout=0)


async def test_an_upload_that_fails_leaves_the_count() -> None:
    uploads = SnapshotUploads()

    with pytest.raises(RuntimeError):
        async with uploads.track(_SERVER, _WORKER_A):
            raise RuntimeError("aborted")

    assert await uploads.settled(_SERVER, _WORKER_A, timeout=0)


async def test_nothing_is_kept_once_the_uploads_have_left() -> None:
    # Nothing per server outlives the uploads, so a deleted server leaves nothing
    # to trim, including after a wait that ran out.
    uploads = SnapshotUploads()

    async with uploads.track(_SERVER, _WORKER_A):
        assert not await uploads.settled(_SERVER, _WORKER_A, timeout=0)

    assert (uploads._in_flight, uploads._left) == ({}, {})


# --- the publish handler ----------------------------------------------------


def _fs_storage(tmp_path: Path) -> Storage:
    return FsStorage(tmp_path)


def _object_storage(tmp_path: Path) -> Storage:
    del tmp_path
    return ObjectStorage(fake_s3_factory(FakeS3Store()))


_BACKENDS = pytest.mark.parametrize(
    "make_storage", [_fs_storage, _object_storage], ids=["fs", "object"]
)


def _tar() -> bytes:
    content = b"packed-by-a"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="world/level.dat")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


async def _no_jar(_c: uuid.UUID, _s: uuid.UUID) -> str | None:
    return None


async def _a_is_assigned(_c: uuid.UUID, _s: uuid.UUID) -> WorkerAssignment | None:
    return WorkerAssignment(worker_id=_WORKER_A, awaiting_hydrate=False)


def _bind(
    app: FastAPI,
    storage: Storage,
    uploads: SnapshotUploads,
    semaphore: asyncio.Semaphore,
) -> None:
    app.dependency_overrides.clear()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_resolved_jar_lookup] = lambda: _no_jar
    app.dependency_overrides[get_assigned_worker_lookup] = lambda: _a_is_assigned
    app.dependency_overrides[get_worker_credential] = lambda: _CREDENTIAL
    app.dependency_overrides[get_transfer_semaphore] = lambda: semaphore
    app.dependency_overrides[get_snapshot_uploads] = lambda: uploads


_COMMUNITY = uuid.uuid4()
_PATH = f"/api/data-plane/communities/{_COMMUNITY}/servers/{_SERVER}/snapshot"
_HEADERS = {"Authorization": f"Bearer {_CREDENTIAL}", "X-Worker-Id": _WORKER_A}


async def _generation(storage: Storage) -> int:
    return await storage.current_generation(CommunityId(_COMMUNITY), ServerId(_SERVER))


# 4 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(4 * 10 + 30)
@_BACKENDS
async def test_an_upload_counts_from_before_its_transfer_slot_wait(
    tmp_path: Path, shared_app: FastAPI, make_storage: Callable[[Path], Storage]
) -> None:
    # An upload queued behind the transfer slots has read nothing yet, and will
    # decide on what it reads once it gets one. A start that hydrated its Worker
    # meanwhile would serve the generation before it.
    storage = make_storage(tmp_path)
    uploads = SnapshotUploads()
    semaphore = asyncio.Semaphore(1)
    _bind(shared_app, storage, uploads, semaphore)

    async with AsyncClient(
        transport=ASGITransport(app=shared_app), base_url="http://test"
    ) as client:
        await semaphore.acquire()
        upload = asyncio.create_task(
            client.post(_PATH, content=_tar(), headers=_HEADERS)
        )
        async with asyncio.timeout(30):
            while await uploads.settled(_SERVER, _WORKER_A, timeout=0):
                await asyncio.sleep(0.01)
        assert not upload.done()

        semaphore.release()
        assert await uploads.settled(_SERVER, _WORKER_A, timeout=60)

        assert (await upload).status_code == 204
    assert await _generation(storage) == 1


class _Server(uvicorn.Server):
    """A uvicorn server that leaves the test process's signal handlers alone."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@contextlib.asynccontextmanager
async def _served(app: Any) -> AsyncIterator[_Server]:
    """Serve ``app`` with the production server on a loopback port of the OS's choice.

    Whether an upload outlives the request that carried it is decided by how the
    server handles a client that goes away, which no in-process ASGI call shows.
    """

    server = _Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            http="h11",
            ws="none",
            lifespan="off",
            log_config=None,
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        yield server
    finally:
        server.should_exit = True
        await task


def _port(server: _Server) -> int:
    port: int = server.servers[0].sockets[0].getsockname()[1]
    return port


def _head(length: int) -> bytes:
    lines = [
        f"POST {_PATH} HTTP/1.1",
        "Host: t",
        f"Content-Length: {length}",
        *(f"{name}: {value}" for name, value in _HEADERS.items()),
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(30):
        while not condition():
            await asyncio.sleep(0.01)


# 4 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(4 * 10 + 30)
@_BACKENDS
async def test_an_upload_whose_worker_left_after_the_whole_body_still_commits(
    tmp_path: Path, shared_app: FastAPI, make_storage: Callable[[Path], Storage]
) -> None:
    # The Worker's deadline ends its request once the API holds the whole body.
    # Nothing cancels the handler, so the upload commits and stays in flight
    # until it has: a start waits for the commit, and for no more than it.
    storage = make_storage(tmp_path)
    uploads = SnapshotUploads()
    _bind(shared_app, storage, uploads, asyncio.Semaphore(1))
    staged = asyncio.Event()
    async with _served(shared_app) as server:
        real_write_snapshot = storage.write_snapshot

        async def _write_snapshot(
            handle: SnapshotHandle,
            stream: AsyncIterator[bytes],
            *,
            expected_base: int | None = None,
        ) -> None:
            await real_write_snapshot(handle, stream, expected_base=expected_base)
            staged.set()
            # Commit only once the server has seen the Worker go.
            await _until(lambda: not server.server_state.connections)

        storage.write_snapshot = _write_snapshot  # type: ignore[method-assign]
        body = _tar()
        _, writer = await asyncio.open_connection("127.0.0.1", _port(server))
        writer.write(_head(len(body)) + body)
        await writer.drain()
        await staged.wait()
        writer.close()
        await writer.wait_closed()

        assert await uploads.settled(_SERVER, _WORKER_A, timeout=60)

    assert await _generation(storage) == 1


# 4 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(4 * 10 + 30)
@_BACKENDS
async def test_an_upload_whose_worker_left_mid_body_aborts_and_leaves_the_count(
    tmp_path: Path, shared_app: FastAPI, make_storage: Callable[[Path], Storage]
) -> None:
    # The Worker's deadline ends its request while the body is still arriving.
    # The handler's next read sees the disconnect, the upload aborts, and a
    # start waiting for it goes on.
    storage = make_storage(tmp_path)
    uploads = SnapshotUploads()
    _bind(shared_app, storage, uploads, asyncio.Semaphore(1))
    began = asyncio.Event()
    async with _served(shared_app) as server:
        real_begin_snapshot = storage.begin_snapshot

        async def _begin_snapshot(
            community_id: CommunityId, server_id: ServerId
        ) -> SnapshotHandle:
            handle = await real_begin_snapshot(community_id, server_id)
            began.set()
            return handle

        storage.begin_snapshot = _begin_snapshot  # type: ignore[method-assign]
        body = _tar()
        _, writer = await asyncio.open_connection("127.0.0.1", _port(server))
        writer.write(_head(len(body)) + body[: len(body) // 2])
        await writer.drain()
        await began.wait()
        assert not await uploads.settled(_SERVER, _WORKER_A, timeout=0)
        writer.close()
        await writer.wait_closed()

        assert await uploads.settled(_SERVER, _WORKER_A, timeout=60)

    assert await _generation(storage) == 0
