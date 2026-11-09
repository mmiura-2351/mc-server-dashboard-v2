"""An awaited handle close still closes once the executor has shut down (#3611).

The storage adapters close a handle off the loop, on an executor worker. Once the
loop's default executor has shut down it refuses that close, and the handle would
stay open. Each case uses its handle, shuts the executor down, then closes: the
handle is closed, and the refusal does not escape.
"""

from __future__ import annotations

import asyncio
import builtins
import io
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import ModuleType
from typing import IO, Any

import pytest

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
    return fs._leased_file_stream(lambda: (_opening(path, opened), lambda: None))


def _fs_pinned_file_stream(path: Path, opened: list[IO[Any]]) -> Any:
    async def _open_file() -> io.BufferedReader:
        return _opening(path, opened)

    return fs._pinned_file_stream(_open_file)


def _object_file_parts(path: Path, _opened: list[IO[Any]]) -> Any:
    return object_store._file_parts(path)


_STREAMS: dict[str, tuple[ModuleType, Callable[[Path, list[IO[Any]]], Any]]] = {
    "fs-file_stream": (fs, _fs_file_stream),
    "fs-leased_file_stream": (fs, _fs_leased_file_stream),
    "fs-pinned_file_stream": (fs, _fs_pinned_file_stream),
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
    "fs-off_loop_file": lambda handle: fs._OffLoopFile(handle).close(),  # type: ignore[arg-type]
    "object-handle_jobs": lambda handle: object_store._HandleJobs().close(handle),  # type: ignore[arg-type]
}


@pytest.mark.parametrize("site", list(_CLOSES))
async def test_a_close_after_the_executor_shuts_down_still_raises_its_fault(
    site: str,
) -> None:
    await asyncio.get_running_loop().shutdown_default_executor()

    with pytest.raises(OSError, match="close failed"):
        await _CLOSES[site](_FailingClose())
