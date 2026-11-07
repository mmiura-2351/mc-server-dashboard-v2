"""Reader leases taken by a caller that is cancelled mid-acquisition (#3571).

Several fs reads take the active-reader lease on an executor worker. Cancelling
the caller does not stop that worker: it goes on to take the lease, and the
release it returns reaches no one, so the snapshot stays pinned against publish
GC and the sweep until the process restarts. The object working-set view has
the same window on the loop: the lease is taken before the client's exit, which
a cancel can still interrupt, and no ``__aexit__`` follows a failed
``__aenter__``.

Each case pauses the acquisition right after the lease is taken, cancels the
caller, lets the acquisition finish, then asserts that the lease is gone and that
a publish plus a sweep reclaim the snapshot.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import (
    ObjectStorage,
    S3Client,
)
from mc_server_dashboard_api.storage.domain.port import Storage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    RelPath,
    ServerId,
)
from tests.storage.fake_s3 import FakeS3Client, FakeS3Store
from tests.storage.helpers import new_scope, tar_stream

_Read = Callable[[Storage, CommunityId, ServerId], Awaitable[object]]


async def publish(
    storage: Storage, community: CommunityId, server: ServerId, files: dict[str, bytes]
) -> None:
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(files))
    await storage.commit_snapshot(handle)


async def _first_hydrate_chunk(
    storage: Storage, community: CommunityId, server: ServerId
) -> object:
    return await storage.open_hydrate_source(community, server).__anext__()


async def _first_file_chunk(
    storage: Storage, community: CommunityId, server: ServerId
) -> object:
    stream = storage.open_file_stream(community, server, RelPath("a.txt"))
    return await stream.__anext__()


async def _create_backup(
    storage: Storage, community: CommunityId, server: ServerId
) -> object:
    return await storage.create_backup_from_current(community, server)


async def _check_health(
    storage: Storage, community: CommunityId, server: ServerId
) -> object:
    return await storage.check_current_health(community, server)


async def _enter_view(
    storage: Storage, community: CommunityId, server: ServerId
) -> object:
    async with storage.open_working_set_view(community, server):
        return None


_FS_READS: list[tuple[str, _Read]] = [
    ("open_hydrate_source", _first_hydrate_chunk),
    ("open_file_stream", _first_file_chunk),
    ("create_backup_from_current", _create_backup),
    ("check_current_health", _check_health),
    ("open_working_set_view", _enter_view),
]


@dataclass
class _Pause:
    leased: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)


def _pause_after_lease(storage: FsStorage, monkeypatch: pytest.MonkeyPatch) -> _Pause:
    """Hold the worker thread taking the reader lease once it holds it."""

    pause = _Pause()
    lease_current = storage._lease_current

    def _leasing(
        community: CommunityId, server: ServerId
    ) -> tuple[Path, Callable[[], None]]:
        leased = lease_current(community, server)
        pause.leased.set()
        pause.release.wait(timeout=10)
        return leased

    monkeypatch.setattr(storage, "_lease_current", _leasing)
    return pause


async def _until(predicate: Callable[[], bool]) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    "read", [r for _, r in _FS_READS], ids=[n for n, _ in _FS_READS]
)
async def test_a_lease_taken_after_its_reader_was_cancelled_is_released(
    read: _Read, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"a.txt": b"OLD"})
    pause = _pause_after_lease(storage, monkeypatch)
    task = asyncio.ensure_future(read(storage, community, server))
    assert await asyncio.to_thread(pause.leased.wait, 10)
    [snapshot] = storage._leases

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pause.release.set()

    await _until(lambda: not storage._is_leased(snapshot))
    assert not storage._is_leased(snapshot)
    await publish(storage, community, server, {"a.txt": b"NEW"})
    storage.sweep()
    assert not snapshot.exists()


async def test_an_object_view_cancelled_after_its_lease_releases_it() -> None:
    store = FakeS3Store()
    leased = asyncio.Event()
    release = asyncio.Event()
    armed = False

    @asynccontextmanager
    async def _factory() -> AsyncIterator[S3Client]:
        yield FakeS3Client(store)
        if armed and storage._leases:
            leased.set()
            await release.wait()

    storage = ObjectStorage(_factory)
    community, server = new_scope()
    await publish(storage, community, server, {"a.txt": b"OLD"})
    armed = True
    task = asyncio.ensure_future(_enter_view(storage, community, server))
    await asyncio.wait_for(leased.wait(), 10)
    armed = False
    [prefix] = storage._leases

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()

    assert not storage._is_leased(prefix)
    await publish(storage, community, server, {"a.txt": b"NEW"})
    await storage.sweep()
    assert not any(key.startswith(prefix) for key in store.objects)


async def test_releasing_a_lease_twice_releases_it_once(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"a.txt": b"a"})
    snapshot, release = storage._lease_current(community, server)
    _, _other = storage._lease_current(community, server)

    release()
    release()

    assert storage._is_leased(snapshot)
