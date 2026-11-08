"""What a cancelled fs read leaves behind once its thread finishes (#3584).

Cancelling a caller does not stop the executor thread doing its open or read. A
handle that thread goes on to open is owned by no one, so it must be closed
explicitly, not left for garbage collection. A fault it goes on to raise has no
caller left to observe it, so it must not reach asyncio's "Future exception was
never retrieved" report either.
"""

from __future__ import annotations

import asyncio
import gc
import io
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from mc_server_dashboard_api.storage.adapters import fs
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import RelPath
from tests.storage.helpers import new_scope, publish, stream_of


class _TrackedReader(io.BufferedReader):
    """A reader that reports its close."""

    def __init__(self, path: Path, closed: threading.Event) -> None:
        super().__init__(io.FileIO(path, "rb"))
        self._closed_event = closed

    def close(self) -> None:
        super().close()
        self._closed_event.set()


@dataclass
class _PausedOpen:
    """Stands in for ``_open_readable_sync``: holds the open until ``resume``."""

    entered: threading.Event = field(default_factory=threading.Event)
    resume: threading.Event = field(default_factory=threading.Event)
    closed: threading.Event = field(default_factory=threading.Event)
    # Held here so only an explicit close, never collection, can close it.
    opened: list[_TrackedReader] = field(default_factory=list)

    def __call__(self, path: Path, _not_found: str) -> io.BufferedReader:
        self.entered.set()
        # No timeout: a hold that let go by itself would let the open finish before
        # the cancel it is held for (issue #3600). The fixture's teardown and the
        # pytest timeout end a hold the test never resumes.
        self.resume.wait()
        handle = _TrackedReader(path, self.closed)
        self.opened.append(handle)
        return handle


@pytest.fixture
async def paused_open() -> AsyncIterator[_PausedOpen]:
    # Async, so its teardown runs before the event loop's: the loop's shutdown
    # waits for the executor thread a hold parks.
    held = _PausedOpen()
    yield held
    held.resume.set()
    for handle in held.opened:
        handle.close()


_FirstChunk = Callable[[FsStorage], Awaitable[Callable[[], Awaitable[bytes]]]]


async def _jar(storage: FsStorage) -> Callable[[], Awaitable[bytes]]:
    key = await storage.put_jar(stream_of(b"JAR"))
    return lambda: storage.open_jar(key).__anext__()


async def _backup(storage: FsStorage) -> Callable[[], Awaitable[bytes]]:
    community, server = new_scope()
    await publish(storage, community, server, {"a.txt": b"A"})
    key = await storage.create_backup_from_current(community, server)
    return lambda: storage.open_backup(community, server, key).__anext__()


async def _view_file(storage: FsStorage) -> Callable[[], Awaitable[bytes]]:
    community, server = new_scope()
    await publish(storage, community, server, {"a.txt": b"A"})

    async def _first_chunk() -> bytes:
        async with storage.open_working_set_view(community, server) as view:
            return await view.open_file_stream(RelPath("a.txt")).__anext__()

    return _first_chunk


_OPENERS: list[tuple[str, _FirstChunk]] = [
    ("open_jar", _jar),
    ("open_backup", _backup),
    ("working_set_view-open_file_stream", _view_file),
]


@pytest.mark.parametrize(
    "arrange", [a for _, a in _OPENERS], ids=[n for n, _ in _OPENERS]
)
async def test_a_cancelled_open_closes_its_handle_once_the_open_finishes(
    arrange: _FirstChunk,
    paused_open: _PausedOpen,
    tmp_path: Path,
    recwarn: pytest.WarningsRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_chunk = await arrange(FsStorage(tmp_path))
    monkeypatch.setattr(fs, "_open_readable_sync", paused_open)
    task = asyncio.ensure_future(first_chunk())
    assert await asyncio.to_thread(paused_open.entered.wait, 10)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    paused_open.resume.set()

    assert await asyncio.to_thread(paused_open.closed.wait, 10)
    assert [handle.closed for handle in paused_open.opened] == [True]
    paused_open.opened.clear()
    gc.collect()
    assert not [w for w in recwarn if issubclass(w.category, ResourceWarning)]


@dataclass
class _FailingListing:
    """Stands in for ``_list_entries``: raises once ``resume`` is set."""

    entered: threading.Event = field(default_factory=threading.Event)
    resume: threading.Event = field(default_factory=threading.Event)
    fault: RuntimeError = field(default_factory=lambda: RuntimeError("listing"))

    def __call__(self, *_args: Any) -> Any:
        self.entered.set()
        self.resume.wait()  # no timeout, as in _PausedOpen (issue #3600)
        raise self.fault


@pytest.fixture
async def failing_listing() -> AsyncIterator[_FailingListing]:  # async, as paused_open
    held = _FailingListing()
    yield held
    held.resume.set()


@pytest.fixture
async def loop_errors() -> AsyncIterator[list[dict[str, Any]]]:
    """What the running loop reports to its exception handler."""

    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    reported: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    yield reported
    loop.set_exception_handler(previous)


def _watch_release(
    storage: FsStorage, monkeypatch: pytest.MonkeyPatch
) -> threading.Event:
    """An event set once the storage holds no reader lease."""

    released = threading.Event()
    release_lease = storage._release_lease

    def _releasing(path: Path) -> None:
        release_lease(path)
        if not storage._is_leased(path):
            released.set()

    monkeypatch.setattr(storage, "_release_lease", _releasing)
    return released


async def test_a_cancelled_readers_worker_fault_is_not_left_unretrieved(
    failing_listing: _FailingListing,
    loop_errors: list[dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"a.txt": b"A"})
    released = _watch_release(storage, monkeypatch)
    monkeypatch.setattr(fs, "_list_entries", failing_listing)

    async def _list() -> object:
        async with storage.open_working_set_view(community, server) as view:
            return await view.list_dir(RelPath("."))

    task = asyncio.ensure_future(_list())
    assert await asyncio.to_thread(failing_listing.entered.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    failing_listing.resume.set()

    assert await asyncio.to_thread(released.wait, 10)
    gc.collect()
    assert loop_errors == []


async def test_an_awaiting_reader_still_receives_its_workers_fault(
    failing_listing: _FailingListing, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"a.txt": b"A"})
    monkeypatch.setattr(fs, "_list_entries", failing_listing)
    failing_listing.resume.set()

    async with storage.open_working_set_view(community, server) as view:
        with pytest.raises(RuntimeError) as raised:
            await view.list_dir(RelPath("."))
    assert raised.value is failing_listing.fault
