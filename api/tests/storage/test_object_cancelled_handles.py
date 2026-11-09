"""What a cancelled object-store archive or spool read/write leaves behind (#3602).

Cancelling a caller does not stop the executor thread running its open, read or
write. So a handle a cancelled open goes on to return must be closed explicitly
once the open finishes, not left for garbage collection; and a handle a cancelled
job is still using must not be closed until that job finishes, since closing under
it could let a reused fd send its I/O to an unrelated file.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import gc
import gzip
import io
import tarfile
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from mc_server_dashboard_api.storage.adapters import object_store
from mc_server_dashboard_api.storage.adapters.object_store import (
    S3Object,
    _archive_file_member_streams,
    _archive_member_parts,
    _file_parts,
    _HandleJobs,
    _write_backup_targz,
)
from tests.storage.helpers import tar_bytes

_REAL_TARFILE_OPEN = tarfile.open


class _Reader(io.BufferedReader):
    """A spool reader whose methods a test can replace on the instance."""


@dataclass
class _Held:
    """The handles a patched opener returned, and a hold on one call to them."""

    entered: threading.Event = field(default_factory=threading.Event)
    resume: threading.Event = field(default_factory=threading.Event)
    closed: threading.Event = field(default_factory=threading.Event)
    # What the held call raises once resumed, if anything.
    fault: BaseException | None = None
    # Held here so only an explicit close, never collection, can close them.
    handles: list[Any] = field(default_factory=list)

    def pausing(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        def _paused(*args: Any, **kwargs: Any) -> Any:
            self.entered.set()
            self.resume.wait()
            if self.fault is not None:
                raise self.fault
            return fn(*args, **kwargs)

        return _paused

    def track(self, handle: Any) -> None:
        close = handle.close

        def _closing() -> None:
            close()
            self.closed.set()

        handle.close = _closing
        self.handles.append(handle)


@pytest.fixture
async def held() -> AsyncIterator[_Held]:
    # Async, so the hold is released before the loop's executor shutdown waits on
    # the parked thread (issue #3600).
    holding = _Held()
    yield holding
    holding.resume.set()


def _opener(
    real: Callable[..., Any], held: _Held, pause: str | None
) -> Callable[..., Any]:
    """``real`` with its result tracked and the ``pause`` call held.

    ``pause`` is ``"open"`` (the open itself), a handle method name, or
    ``"member_read"`` (the first read of a member the tar extracts).
    """

    def _open(*args: Any, **kwargs: Any) -> Any:
        if pause == "open":
            held.pausing(lambda: None)()
        handle = real(*args, **kwargs)
        held.track(handle)
        if pause == "member_read":
            extract = handle.extractfile

            def _extract(member: Any) -> Any:
                member_file = extract(member)
                member_file.read = held.pausing(member_file.read)
                return member_file

            handle.extractfile = _extract
        elif pause not in (None, "open"):
            setattr(handle, pause, held.pausing(getattr(handle, pause)))
        return handle

    return _open


def _patch_tarfile(monkeypatch: pytest.MonkeyPatch, held: _Held, pause: str) -> None:
    monkeypatch.setattr(tarfile, "open", _opener(_REAL_TARFILE_OPEN, held, pause))


def _patch_spool_open(monkeypatch: pytest.MonkeyPatch, held: _Held, pause: str) -> None:
    def _open_spool(path: Path, _mode: str) -> _Reader:
        return _Reader(io.FileIO(path, "rb"))

    monkeypatch.setattr(
        object_store, "open", _opener(_open_spool, held, pause), raising=False
    )


async def _consume_streams(spool: Path) -> None:
    streams = _archive_file_member_streams(spool, "r|gz")
    assert isinstance(streams, AsyncGenerator)
    try:
        async for _name, parts in streams:
            async for _chunk in parts:
                pass
    finally:
        # A real caller's frame drops the walk, whose finalizer closes it.
        await streams.aclose()


class _Client:
    async def get_object(self, _key: str) -> AsyncIterator[bytes]:
        async def _body() -> AsyncIterator[bytes]:
            yield b"A"

        return _body()


# Each site: (patch, the operation under test given a spool path).
_Patch = Callable[[pytest.MonkeyPatch, _Held, str], None]
_Run = Callable[[Path], Awaitable[object]]


async def _member_parts(spool: Path) -> object:
    return await _archive_member_parts(spool, "a.txt", "r:*").__anext__()


async def _file_first_part(spool: Path) -> object:
    return await _file_parts(spool).__anext__()


async def _write_targz(spool: Path) -> None:
    obj = S3Object(key="snap/a.txt", size=1, last_modified=dt.datetime.now(dt.UTC))
    await _write_backup_targz(_Client(), "snap/", [obj], spool)  # type: ignore[arg-type]


_SITES: dict[str, tuple[_Patch, _Run]] = {
    "archive_member_parts": (_patch_tarfile, _member_parts),
    "archive_file_member_streams": (_patch_tarfile, _consume_streams),
    "file_parts": (_patch_spool_open, _file_first_part),
    "write_backup_targz": (_patch_tarfile, _write_targz),
}


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    path = tmp_path / "spool.tar.gz"
    path.write_bytes(gzip.compress(tar_bytes({"a.txt": b"A"})))
    return path


async def _cancel_while_held(run: _Run, spool: Path, held: _Held) -> None:
    """Start ``run``, cancel it once a held call is entered: it raises at once."""

    task = asyncio.ensure_future(run(spool))
    assert await asyncio.to_thread(held.entered.wait, 10)
    task.cancel()
    done, _pending = await asyncio.wait({task}, timeout=10)
    assert task in done
    with pytest.raises(asyncio.CancelledError):
        task.result()


@pytest.mark.parametrize("site", list(_SITES))
async def test_a_cancelled_open_closes_its_handle_once_the_open_finishes(
    site: str,
    held: _Held,
    spool: Path,
    recwarn: pytest.WarningsRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch, run = _SITES[site]
    patch(monkeypatch, held, "open")

    await _cancel_while_held(run, spool, held)
    assert held.handles == []
    held.resume.set()

    assert await asyncio.to_thread(held.closed.wait, 10)
    assert [handle.closed for handle in held.handles] == [True]
    held.handles.clear()
    gc.collect()
    assert not [w for w in recwarn if issubclass(w.category, ResourceWarning)]


_JOBS: list[tuple[str, str]] = [
    ("archive_member_parts", "member_read"),
    ("archive_file_member_streams", "next"),
    ("archive_file_member_streams", "member_read"),
    ("file_parts", "read"),
    ("write_backup_targz", "addfile"),
]


@pytest.mark.parametrize(("site", "job"), _JOBS, ids=[f"{s}-{j}" for s, j in _JOBS])
async def test_a_cancelled_job_keeps_its_handle_open_until_the_job_finishes(
    site: str,
    job: str,
    held: _Held,
    spool: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch, run = _SITES[site]
    patch(monkeypatch, held, job)

    await _cancel_while_held(run, spool, held)
    assert not held.closed.is_set()
    assert [handle.closed for handle in held.handles] == [False]
    held.resume.set()

    assert await asyncio.to_thread(held.closed.wait, 10)
    assert [handle.closed for handle in held.handles] == [True]


async def _until_set(event: threading.Event) -> None:
    """Wait for ``event`` from the loop: its default executor may be shut down."""

    async with asyncio.timeout(10):
        while not event.is_set():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("job", ["open", "read"])
async def test_a_cancelled_handle_is_closed_while_the_executor_shuts_down(
    job: str, held: _Held, spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The loop's teardown shuts its default executor down while cancelled jobs
    # finish, so the close they defer can no longer be submitted to it.
    _patch_spool_open(monkeypatch, held, job)
    loop = asyncio.get_running_loop()

    await _cancel_while_held(_file_first_part, spool, held)
    shutdown = asyncio.ensure_future(loop.shutdown_default_executor())
    try:
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="shutdown"):
            loop.run_in_executor(None, int)
    finally:
        held.resume.set()
    await shutdown

    await _until_set(held.closed)
    assert [handle.closed for handle in held.handles] == [True]


@pytest.fixture
async def loop_errors() -> AsyncIterator[list[dict[str, Any]]]:
    """What the running loop reports to its exception handler."""

    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    reported: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    yield reported
    loop.set_exception_handler(previous)


async def test_a_cancelled_member_reads_fault_is_retrieved_before_its_archive_closes(
    held: _Held,
    spool: Path,
    loop_errors: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A member stream can unwind well before its archive's walk is closed (the
    # multipart uploader awaits its abort in between). A read that fails in that
    # interval has finished by the time the archive closes, and its fault has no
    # caller left to observe it.
    held.fault = OSError("cancelled member read failed")
    _patch_tarfile(monkeypatch, held, "member_read")
    loop = asyncio.get_running_loop()
    held_jobs: list[asyncio.Future[Any]] = []
    submit = loop.run_in_executor

    def _submit(executor: Any, fn: Callable[..., Any], *args: Any) -> Any:
        job = submit(executor, fn, *args)
        if getattr(fn, "__name__", None) == "_paused":
            held_jobs.append(job)
        return job

    monkeypatch.setattr(loop, "run_in_executor", _submit)
    streams = _archive_file_member_streams(spool, "r|gz")
    assert isinstance(streams, AsyncGenerator)
    try:
        _name, parts = await streams.__anext__()

        async def _read() -> object:
            return await parts.__anext__()

        reading = asyncio.ensure_future(_read())
        try:
            assert await asyncio.to_thread(held.entered.wait, 10)
            reading.cancel()
            await asyncio.wait({reading})
            assert reading.cancelled()
        finally:
            held.resume.set()
        await asyncio.wait(held_jobs)
        await asyncio.sleep(0)
    finally:
        await streams.aclose()

    assert [handle.closed for handle in held.handles] == [True]
    del reading
    held_jobs.clear()
    gc.collect()
    assert loop_errors == []


class _Handle:
    def __init__(self) -> None:
        self.closes = 0

    def close(self) -> None:
        self.closes += 1


async def test_handle_jobs_close_their_handle_once() -> None:
    jobs, handle = _HandleJobs(), _Handle()

    await jobs.close(handle)  # type: ignore[arg-type]
    await jobs.close(handle)  # type: ignore[arg-type]

    assert handle.closes == 1


async def test_handle_jobs_refuse_a_job_once_closed() -> None:
    jobs = _HandleJobs()
    await jobs.close(_Handle())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="closed"):
        await jobs.run(int)


async def test_handle_jobs_refuse_a_job_while_an_earlier_one_runs(held: _Held) -> None:
    # A handle's jobs run one at a time: a close follows the last job only.
    jobs = _HandleJobs()
    first = asyncio.ensure_future(jobs.run(held.pausing(int)))
    try:
        assert await asyncio.to_thread(held.entered.wait, 10)
        first.cancel()
        await asyncio.wait({first})
        assert first.cancelled()

        with pytest.raises(RuntimeError, match="earlier job"):
            await jobs.run(int)
    finally:
        held.resume.set()
