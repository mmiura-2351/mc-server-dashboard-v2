"""An object stream closes its response body before its lease goes (issue #3655).

An ``async for`` over the object body does not close the body's iterator when the
loop is left at a ``yield``. A stream closed early therefore released its prefix
lease with the response body still open, and a finalizer closed the body later
with no lease held. Each stream that reads a body under a reader lease must close
the body, then exit its client, then release the lease, on every exit.

The bodies are served through the production ``_iter_body`` over a double of
aiobotocore's ``StreamingBody``, whose ``close`` is synchronous like the real one.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, aclosing, asynccontextmanager
from dataclasses import dataclass, field

import pytest

from mc_server_dashboard_api.storage.adapters.object_client import _iter_body
from mc_server_dashboard_api.storage.adapters.object_store import (
    ObjectStorage,
    S3Client,
)
from mc_server_dashboard_api.storage.domain.errors import ObjectStoreUnavailableError
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    RelPath,
    ServerId,
)
from tests.storage.fake_s3 import FakeS3Client, FakeS3Store
from tests.storage.helpers import new_scope, tar_stream

_NAME = "a.bin"
_DATA = b"0123456789abcdef"
_STEP = 4
_ORDER = ["close", "client-exit", "release"]


@dataclass
class _Rig:
    """What the watched body does, and the ordered log of the stream's exit."""

    events: list[str] = field(default_factory=list)
    armed: bool = False
    served: asyncio.Event = field(default_factory=asyncio.Event)
    stall_after: int | None = None
    stalled: asyncio.Event = field(default_factory=asyncio.Event)
    fail_after: int | None = None
    on_close: Callable[[], None] | None = None
    exit_suspends: bool = False

    def log(self, event: str) -> None:
        if self.armed:
            self.events.append(event)


class _Body:
    """An aiobotocore ``StreamingBody`` double: bounded reads, a logged sync close."""

    def __init__(self, data: bytes, rig: _Rig) -> None:
        self._data = data
        self._rig = rig
        self._reads = 0

    async def read(self, _amt: int) -> bytes:
        if self._reads == self._rig.stall_after:
            self._rig.stalled.set()
            await asyncio.Event().wait()
        if self._reads == self._rig.fail_after:
            raise OSError("injected body read fault")
        chunk = self._data[self._reads * _STEP : (self._reads + 1) * _STEP]
        self._reads += 1
        self._rig.served.set()
        return chunk

    def close(self) -> None:
        self._rig.log("close")
        if self._rig.on_close is not None:
            self._rig.on_close()


class _BodyClient(FakeS3Client):
    def __init__(self, store: FakeS3Store, rig: _Rig) -> None:
        super().__init__(store)
        self._rig = rig

    async def get_object(
        self, key: str, byte_range: tuple[int, int] | None = None
    ) -> AsyncGenerator[bytes, None]:
        if not key.endswith("/" + _NAME):
            return await super().get_object(key, byte_range)
        return _iter_body(_Body(self._store.objects[key], self._rig))


async def _storage(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ObjectStorage, CommunityId, ServerId, _Rig]:
    store = FakeS3Store()
    rig = _Rig()

    @asynccontextmanager
    async def _factory() -> AsyncIterator[S3Client]:
        try:
            yield _BodyClient(store, rig)
        finally:
            try:
                if rig.exit_suspends:
                    # A real client's exit awaits, so a cancel can land in it.
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                rig.log("exit-cancelled")
                raise
            finally:
                rig.log("client-exit")

    storage = ObjectStorage(_factory)
    community, server = new_scope()
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream({_NAME: _DATA}))
    await storage.commit_snapshot(handle)
    release = storage._release_lease

    def _logged_release(prefix: str) -> None:
        rig.log("release")
        release(prefix)

    monkeypatch.setattr(storage, "_release_lease", _logged_release)
    return storage, community, server, rig


_Open = Callable[
    [ObjectStorage, CommunityId, ServerId, _Rig],
    AbstractAsyncContextManager[AsyncIterator[bytes]],
]


@asynccontextmanager
async def _file_stream(
    storage: ObjectStorage, community: CommunityId, server: ServerId, rig: _Rig
) -> AsyncIterator[AsyncIterator[bytes]]:
    rig.armed = True
    yield storage.open_file_stream(community, server, RelPath(_NAME))


@asynccontextmanager
async def _hydrate_stream(
    storage: ObjectStorage, community: CommunityId, server: ServerId, rig: _Rig
) -> AsyncIterator[AsyncIterator[bytes]]:
    rig.armed = True
    yield storage.open_hydrate_source(community, server)


@asynccontextmanager
async def _view_stream(
    storage: ObjectStorage, community: CommunityId, server: ServerId, rig: _Rig
) -> AsyncIterator[AsyncIterator[bytes]]:
    # The view's lease is the one the view stream reads under; it goes on the
    # view's exit, so the stream's body must be closed by then.
    async with storage.open_working_set_view(community, server) as view:
        rig.armed = True
        yield view.open_file_stream(RelPath(_NAME))


_STREAMS = pytest.mark.parametrize(
    "open_stream",
    [_file_stream, _hydrate_stream, _view_stream],
    ids=["open_file_stream", "open_hydrate_source", "working_set_view"],
)


async def _until_served(stream: AsyncIterator[bytes], rig: _Rig) -> None:
    while not rig.served.is_set():
        await stream.__anext__()


@_STREAMS
async def test_an_early_close_closes_the_body_before_the_release(
    monkeypatch: pytest.MonkeyPatch, open_stream: _Open
) -> None:
    storage, community, server, rig = await _storage(monkeypatch)

    async with open_stream(storage, community, server, rig) as stream:
        await _until_served(stream, rig)
        await stream.aclose()  # type: ignore[attr-defined]

    assert rig.events == _ORDER


@_STREAMS
async def test_a_cancel_during_iteration_closes_the_body_before_the_release(
    monkeypatch: pytest.MonkeyPatch, open_stream: _Open
) -> None:
    storage, community, server, rig = await _storage(monkeypatch)
    rig.stall_after = 1

    async def _consume() -> None:
        async with open_stream(storage, community, server, rig) as stream:
            async for _chunk in stream:
                pass

    task = asyncio.ensure_future(_consume())
    await asyncio.wait_for(rig.stalled.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert rig.events == _ORDER


@_STREAMS
async def test_a_read_fault_closes_the_body_before_the_release(
    monkeypatch: pytest.MonkeyPatch, open_stream: _Open
) -> None:
    storage, community, server, rig = await _storage(monkeypatch)
    rig.fail_after = 1

    with pytest.raises(ObjectStoreUnavailableError):
        async with open_stream(storage, community, server, rig) as stream:
            async for _chunk in stream:
                pass

    assert rig.events == _ORDER


@_STREAMS
async def test_a_cancel_during_a_held_body_close_keeps_the_lease_until_it_finishes(
    monkeypatch: pytest.MonkeyPatch, open_stream: _Open
) -> None:
    # The body's close is synchronous, so holding it holds the loop. A helper
    # thread observes the lease ledger (lock-guarded) while the close is held,
    # requests a second cancel, then lets the close finish. That cancel lands in
    # the client's exit, the one await between the close and the release.
    storage, community, server, rig = await _storage(monkeypatch)
    rig.exit_suspends = True
    loop = asyncio.get_running_loop()
    entered = threading.Event()
    resume = threading.Event()
    held_during_close: list[bool] = []

    def _hold() -> None:
        entered.set()
        assert resume.wait(10), "the helper never resumed the held close"

    rig.on_close = _hold

    async def _consume() -> None:
        async with open_stream(storage, community, server, rig) as stream:
            async with aclosing(stream):  # type: ignore[type-var]
                await _until_served(stream, rig)
                await asyncio.Event().wait()  # the consumer parks mid-stream

    task = asyncio.ensure_future(_consume())

    def _helper() -> None:
        try:
            if entered.wait(10):
                with storage._lease_lock:
                    held_during_close.append(bool(storage._leases))
                loop.call_soon_threadsafe(task.cancel)
        finally:
            resume.set()

    await asyncio.wait_for(rig.served.wait(), 10)
    helper = threading.Thread(target=_helper)
    helper.start()
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await asyncio.to_thread(helper.join, 10)

    assert held_during_close == [True]
    assert rig.events == ["close", "exit-cancelled", "client-exit", "release"]
    assert not storage._leases


@_STREAMS
async def test_a_body_close_fault_is_raised_once_and_the_lease_still_goes(
    monkeypatch: pytest.MonkeyPatch, open_stream: _Open
) -> None:
    storage, community, server, rig = await _storage(monkeypatch)
    fault = OSError("injected body close fault")

    def _fail() -> None:
        raise fault

    rig.on_close = _fail

    with pytest.raises(OSError) as excinfo:
        async with open_stream(storage, community, server, rig) as stream:
            await _until_served(stream, rig)
            await stream.aclose()  # type: ignore[attr-defined]

    assert excinfo.value is fault
    assert rig.events == _ORDER
    assert not storage._leases
