"""An awaited handle close still closes once the executor has shut down (#3611).

The storage adapters close a handle off the loop, on an executor worker. Once the
loop's default executor has shut down it refuses that close, and the handle would
stay open. Each case uses its handle, shuts the executor down, then closes: the
handle is closed, and the refusal does not escape.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import io
import json
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import IO, Any

import pytest

from mc_server_dashboard_api.logging import JsonFormatter, correlation_id
from mc_server_dashboard_api.storage.adapters import fs, object_store

_PAYLOAD = b"payload"


def _recording_opens(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType
) -> list[IO[Any]]:
    """Record each file ``module`` opens, strongly held so only a close closes it."""

    opened: list[IO[Any]] = []

    def _open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = builtins.open(file, mode, *args, **kwargs)
        opened.append(handle)
        return handle

    monkeypatch.setattr(module, "open", _open, raising=False)
    return opened


def _opening(path: Path, opened: list[IO[Any]]) -> io.BufferedReader:
    handle = builtins.open(path, "rb")
    opened.append(handle)
    return handle


def _fs_file_stream(path: Path, _opened: list[IO[Any]]) -> Any:
    return fs._file_stream(path, not_found="missing")


def _fs_leased_file_stream(path: Path, opened: list[IO[Any]]) -> Any:
    async def _open_source() -> tuple[io.BufferedReader, Callable[[], None]]:
        return _opening(path, opened), lambda: None

    return fs._leased_file_stream(_open_source)


def _object_file_parts(path: Path, _opened: list[IO[Any]]) -> Any:
    return object_store._file_parts(path)


_STREAMS: dict[str, tuple[ModuleType, Callable[[Path, list[IO[Any]]], Any]]] = {
    "fs-file_stream": (fs, _fs_file_stream),
    "fs-leased_file_stream": (fs, _fs_leased_file_stream),
    "object-file_parts": (object_store, _object_file_parts),
}


@pytest.mark.parametrize("site", list(_STREAMS))
async def test_a_stream_closed_after_the_executor_shuts_down_closes_its_handle(
    site: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, make = _STREAMS[site]
    path = tmp_path / "stored.bin"
    path.write_bytes(_PAYLOAD)
    opened = _recording_opens(monkeypatch, module)
    stream: AsyncIterator[bytes] = make(path, opened)
    assert await anext(stream) == _PAYLOAD

    await asyncio.get_running_loop().shutdown_default_executor()
    await stream.aclose()  # type: ignore[attr-defined]

    assert [handle.closed for handle in opened] == [True]


async def test_a_write_finished_after_the_executor_shuts_down_closes_its_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "written.bin"
    opened = _recording_opens(monkeypatch, fs)

    async with fs._writing_off_loop(path) as out:
        await out.write(_PAYLOAD)
        await asyncio.get_running_loop().shutdown_default_executor()

    assert [handle.closed for handle in opened] == [True]
    assert path.read_bytes() == _PAYLOAD


class _FailingClose:
    def close(self) -> None:
        raise OSError("close failed")


_CLOSES: dict[str, Callable[[_FailingClose], Awaitable[None]]] = {
    "fs-off_loop_file": lambda handle: fs._OffLoopFile(handle, lambda: None).close(),  # type: ignore[arg-type]
    "object-handle_jobs": lambda handle: object_store._HandleJobs().close(handle),  # type: ignore[arg-type]
}


@pytest.mark.parametrize("site", list(_CLOSES))
async def test_a_close_after_the_executor_shuts_down_still_raises_its_fault(
    site: str,
) -> None:
    await asyncio.get_running_loop().shutdown_default_executor()

    with pytest.raises(OSError, match="close failed"):
        await _CLOSES[site](_FailingClose())


class _HeldFailingClose:
    """A close held until released, which then fails on its thread."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.thread: threading.Thread | None = None
        self.closes = 0

    def close(self) -> None:
        self.closes += 1
        self.thread = threading.current_thread()
        self.entered.set()
        self.release.wait()
        raise OSError("close failed")

    async def until_entered(self) -> None:
        async with asyncio.timeout(10):
            while not self.entered.is_set():
                await asyncio.sleep(0.01)

    def finish(self) -> None:
        """Release the close and wait until its thread has reported the fault."""

        self.release.set()
        assert self.thread is not None
        self.thread.join(10)
        assert not self.thread.is_alive()


class _FormattedAtEmit(logging.Handler):
    """Format each record as it is emitted, so on the emitting thread's context."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.setFormatter(JsonFormatter())
        self.lines: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


@pytest.fixture
def close_faults() -> Iterator[list[dict[str, Any]]]:
    handler = _FormattedAtEmit()
    logger = logging.getLogger(fs.__name__)
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


async def _cancel_a_held_close(handle: _HeldFailingClose) -> None:
    await asyncio.get_running_loop().shutdown_default_executor()
    closing = asyncio.create_task(fs._close_off_loop(handle))
    await handle.until_entered()
    closing.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await closing


def test_a_cancelled_close_fault_finished_after_the_loop_stops_is_logged_once(
    close_faults: list[dict[str, Any]],
) -> None:
    """The caller is gone and the loop never turns again: the fault is still logged.

    The loop has stopped but not closed when the held close fails, so a settlement
    handed to the loop is accepted, then dropped by ``loop.close()``.
    """

    handle = _HeldFailingClose()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_cancel_a_held_close(handle))
        handle.finish()
    finally:
        handle.release.set()
        loop.close()

    assert [line["message"] for line in close_faults] == [
        f"could not close the abandoned file {handle}: close failed"
    ]


async def test_a_close_fault_in_before_its_caller_is_cancelled_is_logged_once(
    close_faults: list[dict[str, Any]],
) -> None:
    """The close failed and queued its wake, then the caller is cancelled first."""

    handle = _HeldFailingClose()
    handle.release.set()
    await asyncio.get_running_loop().shutdown_default_executor()
    closing = asyncio.create_task(fs._close_off_loop(handle))
    await asyncio.sleep(0)  # the task starts the close's thread and awaits it
    handle.finish()  # blocks the loop, so the wake stays queued
    closing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await closing
    assert [line["message"] for line in close_faults] == [
        f"could not close the abandoned file {handle}: close failed"
    ]


class _HeldWake:
    """Holds a close's thread at its wake, before the loop checks it is closed."""

    def __init__(self, handle: _HeldFailingClose) -> None:
        self.handle = handle
        self.reached = threading.Event()
        self.resume = threading.Event()

    def install(self, loop: asyncio.AbstractEventLoop) -> None:
        wake = loop.call_soon_threadsafe

        def _held(*args: Any) -> Any:
            if threading.current_thread() is self.handle.thread:
                self.reached.set()
                self.resume.wait()
            return wake(*args)

        setattr(loop, "call_soon_threadsafe", _held)


async def _cancel_a_close_held_at_its_wake(held: _HeldWake) -> None:
    loop = asyncio.get_running_loop()
    await loop.shutdown_default_executor()
    held.install(loop)
    closing = asyncio.create_task(fs._close_off_loop(held.handle))
    async with asyncio.timeout(10):
        while not held.reached.is_set():
            await asyncio.sleep(0.01)
    closing.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await closing


def test_a_close_fault_logged_on_cancel_is_not_logged_again_at_a_failed_wake(
    close_faults: list[dict[str, Any]],
) -> None:
    """The close failed and its caller is cancelled before its wake reaches the loop,
    which then closes: the cancelled caller logs the fault, the failed wake does not.
    """

    handle = _HeldFailingClose()
    handle.release.set()
    held = _HeldWake(handle)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_cancel_a_close_held_at_its_wake(held))
        loop.close()
        held.resume.set()
        handle.finish()
    finally:
        held.resume.set()
        loop.close()

    assert handle.closes == 1
    assert [line["message"] for line in close_faults] == [
        f"could not close the abandoned file {handle}: close failed"
    ]


async def _abandon_a_held_close(handle: _HeldFailingClose) -> None:
    await asyncio.get_running_loop().shutdown_default_executor()
    fs._close_abandoned(handle)
    await handle.until_entered()


async def _leave_a_held_close_pending(handle: _HeldFailingClose) -> None:
    await asyncio.get_running_loop().shutdown_default_executor()
    # Stepped to its await and never resumed, as a pending task its loop drops is.
    fs._close_off_loop(handle).send(None)
    await handle.until_entered()


async def _in_context(
    leave: Callable[[_HeldFailingClose], Awaitable[None]], handle: _HeldFailingClose
) -> None:
    correlation_id.set("review-3613")
    await leave(handle)


# Each leaves a held close on its own thread, the executor having shut down, with
# no caller left to raise its fault: abandoned outright, its awaiting caller
# cancelled, or its caller's loop closed while it still waited.
_UNAWAITED: dict[str, tuple[Callable[[_HeldFailingClose], Awaitable[None]], bool]] = {
    "abandoned": (_abandon_a_held_close, False),
    "cancelled": (_cancel_a_held_close, False),
    "loop-closed": (_leave_a_held_close_pending, True),
}


@pytest.mark.parametrize("case", list(_UNAWAITED))
def test_a_close_fault_logged_off_the_loop_carries_the_callers_correlation_id(
    case: str, close_faults: list[dict[str, Any]]
) -> None:
    leave, close_first = _UNAWAITED[case]
    handle = _HeldFailingClose()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_in_context(leave, handle))
        if close_first:
            loop.close()
        handle.finish()
    finally:
        handle.release.set()
        loop.close()

    assert [line.get("correlation_id") for line in close_faults] == ["review-3613"]


class _RecordedClose:
    """A real file whose close is recorded in ``events``."""

    def __init__(self, path: Path, events: list[str]) -> None:
        self._handle = builtins.open(path, "rb")
        self._events = events
        self.name = self._handle.name
        self.read = self._handle.read

    def close(self) -> None:
        self._handle.close()
        self._events.append("close")


def _no_thread_starts(_thread: threading.Thread) -> None:
    raise RuntimeError("can't start new thread")


async def test_a_leased_stream_whose_close_cannot_start_closes_then_releases_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No worker and no thread will take the close: the stream still owns cleanup.

    It closes the handle, then releases its lease, each once (PR #3646 review B1).
    """

    path = tmp_path / "stored.bin"
    path.write_bytes(_PAYLOAD)
    events: list[str] = []

    async def _open_source() -> tuple[Any, Callable[[], None]]:
        return _RecordedClose(path, events), lambda: events.append("release")

    stream = fs._leased_file_stream(_open_source)
    assert await anext(stream) == _PAYLOAD
    await asyncio.get_running_loop().shutdown_default_executor()

    with monkeypatch.context() as mp:
        mp.setattr(threading.Thread, "start", _no_thread_starts)
        await stream.aclose()  # type: ignore[attr-defined]
        assert events == ["close", "release"]
        await stream.aclose()  # type: ignore[attr-defined]

    assert events == ["close", "release"]
