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
    # Held here so only an explicit close, never collection, can close them.
    handles: list[Any] = field(default_factory=list)

    def pausing(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        def _paused(*args: Any, **kwargs: Any) -> Any:
            self.entered.set()
            self.resume.wait()
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
