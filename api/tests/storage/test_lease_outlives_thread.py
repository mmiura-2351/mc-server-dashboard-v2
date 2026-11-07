"""A reader lease outlives the thread reading its snapshot (#3577).

Several fs reads run lease-protected work on a thread. Cancelling the caller does
not stop that thread, so a caller that releases the lease as it unwinds lets a
publish or sweep reclaim the snapshot while the thread is still reading it.

Each case pauses the thread inside its read, cancels the caller, then attempts a
reclaim: the snapshot must survive while the thread is paused. Once the thread is
let go, the lease is released and the reclaim proceeds.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from mc_server_dashboard_api.storage.adapters import fs
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    RelPath,
    ServerId,
)
from mc_server_dashboard_api.storage.integrity.region import check_working_set
from tests.storage.helpers import new_scope, publish


@dataclass
class _Pause:
    entered: threading.Event = field(default_factory=threading.Event)
    resume: threading.Event = field(default_factory=threading.Event)

    def once(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """``fn``, holding its first call until ``resume`` is set."""

        armed = threading.Lock()

        def _paused(*args: Any, **kwargs: Any) -> Any:
            if armed.acquire(blocking=False):
                self.entered.set()
                self.resume.wait(timeout=10)
            return fn(*args, **kwargs)

        return _paused


@pytest.fixture
def pause() -> Iterator[_Pause]:
    held = _Pause()
    yield held
    held.resume.set()


def _watch_release(
    storage: FsStorage, snapshot: Path, monkeypatch: pytest.MonkeyPatch
) -> threading.Event:
    """An event set once ``snapshot`` holds no reader lease."""

    released = threading.Event()
    release_lease = storage._release_lease

    def _releasing(path: Path) -> None:
        release_lease(path)
        if path == snapshot and not storage._is_leased(snapshot):
            released.set()

    monkeypatch.setattr(storage, "_release_lease", _releasing)
    return released


_Read = Callable[[FsStorage, CommunityId, ServerId], Awaitable[object]]
_Arm = Callable[[FsStorage, _Pause, pytest.MonkeyPatch], None]


def _in_region_scan(storage: FsStorage, pause: _Pause, mp: pytest.MonkeyPatch) -> None:
    mp.setattr(fs, "check_working_set", pause.once(check_working_set))


def _in_archive_write(
    storage: FsStorage, pause: _Pause, mp: pytest.MonkeyPatch
) -> None:
    mp.setattr(
        storage, "_write_backup_archive", pause.once(storage._write_backup_archive)
    )


def _in_listing(storage: FsStorage, pause: _Pause, mp: pytest.MonkeyPatch) -> None:
    mp.setattr(fs, "_list_entries", pause.once(fs._list_entries))


def _in_open(storage: FsStorage, pause: _Pause, mp: pytest.MonkeyPatch) -> None:
    mp.setattr(fs, "_open_readable_sync", pause.once(fs._open_readable_sync))


async def _check_health(
    storage: FsStorage, community: CommunityId, server: ServerId
) -> object:
    return await storage.check_current_health(community, server)


async def _create_backup(
    storage: FsStorage, community: CommunityId, server: ServerId
) -> object:
    return await storage.create_backup_from_current(community, server)


async def _view_list_dir(
    storage: FsStorage, community: CommunityId, server: ServerId
) -> object:
    async with storage.open_working_set_view(community, server) as view:
        return await view.list_dir(RelPath("."))


async def _view_file_chunk(
    storage: FsStorage, community: CommunityId, server: ServerId
) -> object:
    async with storage.open_working_set_view(community, server) as view:
        return await view.open_file_stream(RelPath("a.txt")).__anext__()


async def _file_chunk(
    storage: FsStorage, community: CommunityId, server: ServerId
) -> object:
    return await storage.open_file_stream(
        community, server, RelPath("a.txt")
    ).__anext__()


_THREADED_READS: list[tuple[str, _Read, _Arm]] = [
    ("check_current_health", _check_health, _in_region_scan),
    ("create_backup_from_current-scan", _create_backup, _in_region_scan),
    ("create_backup_from_current-pack", _create_backup, _in_archive_write),
    ("working_set_view-list_dir", _view_list_dir, _in_listing),
    ("working_set_view-open_file_stream", _view_file_chunk, _in_open),
    ("open_file_stream", _file_chunk, _in_open),
]


async def _assert_reclaim_waits_for_the_thread(
    storage: FsStorage,
    community: CommunityId,
    server: ServerId,
    snapshot: Path,
    pause: _Pause,
    released: threading.Event,
) -> None:
    try:
        await publish(storage, community, server, {"a.txt": b"NEW"})
        storage.sweep()
        assert snapshot.exists()
    finally:
        pause.resume.set()

    assert await asyncio.to_thread(released.wait, 10)
    storage.sweep()
    assert not snapshot.exists()


@pytest.mark.parametrize(
    ("read", "arm"),
    [(r, a) for _, r, a in _THREADED_READS],
    ids=[n for n, _, _ in _THREADED_READS],
)
async def test_a_cancelled_reader_keeps_its_lease_until_its_thread_finishes(
    read: _Read,
    arm: _Arm,
    pause: _Pause,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"a.txt": b"OLD"})
    snapshot = storage._current_dir(community, server)
    released = _watch_release(storage, snapshot, monkeypatch)
    arm(storage, pause, monkeypatch)
    task = asyncio.ensure_future(read(storage, community, server))
    assert await asyncio.to_thread(pause.entered.wait, 10)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _assert_reclaim_waits_for_the_thread(
        storage, community, server, snapshot, pause, released
    )


async def test_a_hydrate_stream_keeps_its_lease_until_its_tar_writer_finishes(
    pause: _Pause, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path, tar_member_hook=pause.once(lambda _child: None))
    community, server = new_scope()
    await publish(storage, community, server, {"a.txt": b"OLD"})
    snapshot = storage._current_dir(community, server)
    released = _watch_release(storage, snapshot, monkeypatch)
    source = storage.open_hydrate_source(community, server)
    task = asyncio.ensure_future(source.__anext__())
    assert await asyncio.to_thread(pause.entered.wait, 10)

    # The first cancel leaves the stream waiting for its paused writer to finish;
    # a second one stops that wait too.
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _assert_reclaim_waits_for_the_thread(
        storage, community, server, snapshot, pause, released
    )
