"""A reader lease outlives the thread reading its snapshot (#3577).

Several fs reads run lease-protected work on a thread. Cancelling the caller does
not stop that thread, so a caller that releases the lease as it unwinds lets a
publish or sweep reclaim the snapshot while the thread is still reading it.

Each case pauses the thread inside its read, or inside the file close that ends
it (#3618), cancels the caller, then attempts a reclaim: the snapshot must
survive while the thread is paused. Once the thread is
let go, the lease is released and the reclaim proceeds.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from mc_server_dashboard_api.storage.adapters import fs
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.errors import NotFoundError
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
        """``fn``, holding its first call until ``resume`` is set.

        The hold has no timeout: one that let go by itself would let the thread
        finish and release its lease while the test still relies on it, faking a
        lifetime bug (issue #3600). The fixture's teardown and the pytest timeout
        end a hold the test never resumes.
        """

        armed = threading.Lock()

        def _paused(*args: Any, **kwargs: Any) -> Any:
            if armed.acquire(blocking=False):
                self.entered.set()
                self.resume.wait()
            return fn(*args, **kwargs)

        return _paused


@pytest.fixture
async def pause() -> AsyncIterator[_Pause]:
    # Async, so its teardown runs before the event loop's: the loop's shutdown
    # waits for the executor thread a hold parks.
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


class _CloseHeld:
    """A real file whose close is held by ``pause`` (issue #3618)."""

    def __init__(self, handle: Any, pause: _Pause) -> None:
        self.name = handle.name
        self.read = handle.read
        self.close = pause.once(handle.close)


def _in_close(storage: FsStorage, pause: _Pause, mp: pytest.MonkeyPatch) -> None:
    open_readable = fs._open_readable_sync

    def _opening(path: Path, not_found: str) -> _CloseHeld:
        return _CloseHeld(open_readable(path, not_found), pause)

    mp.setattr(fs, "_open_readable_sync", _opening)


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


async def _view_file_drained(
    storage: FsStorage, community: CommunityId, server: ServerId
) -> object:
    async with storage.open_working_set_view(community, server) as view:
        return [chunk async for chunk in view.open_file_stream(RelPath("a.txt"))]


async def _file_drained(
    storage: FsStorage, community: CommunityId, server: ServerId
) -> object:
    stream = storage.open_file_stream(community, server, RelPath("a.txt"))
    return [chunk async for chunk in stream]


_THREADED_READS: list[tuple[str, _Read, _Arm]] = [
    ("check_current_health", _check_health, _in_region_scan),
    ("create_backup_from_current-scan", _create_backup, _in_region_scan),
    ("create_backup_from_current-pack", _create_backup, _in_archive_write),
    ("working_set_view-list_dir", _view_list_dir, _in_listing),
    ("working_set_view-open_file_stream", _view_file_chunk, _in_open),
    ("open_file_stream", _file_chunk, _in_open),
    ("working_set_view-open_file_stream-close", _view_file_drained, _in_close),
    ("open_file_stream-close", _file_drained, _in_close),
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

    # A bound, not a delay: the thread's fsyncs can stall on a loaded host.
    assert await asyncio.to_thread(released.wait, 120)
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


class _CountedRelease:
    """A lease release that counts its calls, from any thread."""

    def __init__(self) -> None:
        self.calls = 0
        self.called = threading.Event()

    def __call__(self) -> None:
        self.calls += 1
        self.called.set()


async def test_a_file_stream_cancelled_in_its_close_releases_once_it_finishes(
    pause: _Pause, tmp_path: Path
) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"OLD")
    release = _CountedRelease()

    async def _open() -> tuple[Any, Callable[[], None]]:
        return _CloseHeld(open(path, "rb"), pause), release

    async def _drain() -> list[bytes]:
        return [chunk async for chunk in fs._leased_file_stream(_open)]

    task = asyncio.ensure_future(_drain())
    assert await asyncio.to_thread(pause.entered.wait, 10)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert release.calls == 0

    pause.resume.set()
    # A bound, not a delay: the close's thread can stall on a loaded host.
    assert await asyncio.to_thread(release.called.wait, 120)
    assert release.calls == 1


class _FailingRead:
    def __init__(self, path: Path) -> None:
        self._handle = open(path, "rb")
        self.name = self._handle.name
        self.close = self._handle.close

    def read(self, _size: int) -> bytes:
        raise OSError("read failed")


@pytest.mark.parametrize("fails", [False, True], ids=["drained", "read-fault"])
async def test_a_finished_file_stream_releases_its_lease_once(
    fails: bool, tmp_path: Path
) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"OLD")
    release = _CountedRelease()

    async def _open() -> tuple[Any, Callable[[], None]]:
        return (_FailingRead(path) if fails else open(path, "rb")), release

    stream = fs._leased_file_stream(_open)
    if fails:
        with pytest.raises(OSError, match="read failed"):
            await anext(stream)
    else:
        assert [chunk async for chunk in stream] == [b"OLD"]

    assert release.calls == 1


class _SweptAtClose:
    """A real file whose close first sweeps and records whether ``snapshot`` survived.

    The close of a cancelled open's late handle runs on the loop, so it cannot be
    held while a sweep runs: the sweep runs at the close's entry instead.
    """

    def __init__(
        self, handle: Any, storage: FsStorage, snapshot: Path, survived: list[bool]
    ) -> None:
        self.name = handle.name
        self._handle = handle
        self._storage = storage
        self._snapshot = snapshot
        self._survived = survived

    def close(self) -> None:
        self._storage.sweep()
        self._survived.append(self._snapshot.exists())
        self._handle.close()


def _lease_ledger(
    storage: FsStorage, snapshot: Path, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Each lease taken (``+``) and released (``-``) on ``snapshot``, in order."""

    ledger: list[str] = []
    acquire, release = storage._acquire_lease, storage._release_lease

    def _acquiring(path: Path) -> None:
        acquire(path)
        if path == snapshot:
            ledger.append("+")

    def _releasing(path: Path) -> None:
        if path == snapshot:
            ledger.append("-")
        release(path)

    monkeypatch.setattr(storage, "_acquire_lease", _acquiring)
    monkeypatch.setattr(storage, "_release_lease", _releasing)
    return ledger


@pytest.mark.parametrize("opens", [True, False], ids=["late-handle", "open-fault"])
async def test_a_view_stream_cancelled_in_its_open_holds_its_lease_through_the_cleanup(
    opens: bool, pause: _Pause, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream's lease passes to the cleanup of its cancelled open (PR #3646 B2).

    A late handle is closed under it, and each lease is released once either way.
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"a.txt": b"OLD"})
    snapshot = storage._current_dir(community, server)
    ledger = _lease_ledger(storage, snapshot, monkeypatch)
    released = _watch_release(storage, snapshot, monkeypatch)
    survived: list[bool] = []
    open_readable = fs._open_readable_sync

    def _opening(path: Path, not_found: str) -> _SweptAtClose:
        if not opens:
            raise NotFoundError(not_found)
        return _SweptAtClose(
            open_readable(path, not_found), storage, snapshot, survived
        )

    monkeypatch.setattr(fs, "_open_readable_sync", pause.once(_opening))
    task = asyncio.ensure_future(_view_file_chunk(storage, community, server))
    assert await asyncio.to_thread(pause.entered.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    try:
        await publish(storage, community, server, {"a.txt": b"NEW"})
        storage.sweep()
        assert snapshot.exists()
    finally:
        pause.resume.set()

    # A bound, not a delay: the open's thread can stall on a loaded host.
    assert await asyncio.to_thread(released.wait, 120)
    assert survived == ([True] if opens else [])
    assert ledger.count("+") == ledger.count("-")
    storage.sweep()
    assert not snapshot.exists()
