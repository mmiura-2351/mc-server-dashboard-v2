"""A consumer that stops reading an object body closes it at once (issue #3671).

An ``async for`` over an object body does not close the body's iterator when the
loop is left early: a consumer closed at a ``yield``, or a fault raised in the loop
body. #3655 closed the bodies read under a reader lease. The reads here hold no
lease, but each one still closes its body before its client exits instead of
leaving it parked until a finalizer runs.

The watched bodies are held by the test, so no finalizer can close them behind it:
a body is closed only if its consumer closes it.
"""

from __future__ import annotations

import errno
import gzip
import hashlib
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field

import pytest

from mc_server_dashboard_api.servers.adapters.plugin_cache_store import (
    ObjectPluginCacheStore,
)
from mc_server_dashboard_api.servers.adapters.resource_pack_store import (
    ObjectResourcePackStore,
)
from mc_server_dashboard_api.servers.domain.resource_pack import ResourcePackId
from mc_server_dashboard_api.storage.adapters.fs import _OffLoopFile
from mc_server_dashboard_api.storage.adapters.object_store import (
    ObjectStorage,
    S3Client,
    S3ClientFactory,
)
from tests.storage.fake_s3 import FakeS3Client, FakeS3Store
from tests.storage.faulty_s3 import Faults, _stalling
from tests.storage.helpers import new_scope, stream_of, tar_bytes

_DATA = gzip.compress(tar_bytes({"a.bin": b"0123456789abcdef" * 64}))


@dataclass
class _Rig:
    events: list[str] = field(default_factory=list)
    # Every body handed out, held so that only its consumer can close it.
    bodies: list[AsyncGenerator[bytes, None]] = field(default_factory=list)


class _TrackingClient(FakeS3Client):
    def __init__(self, store: FakeS3Store, rig: _Rig) -> None:
        super().__init__(store)
        self._rig = rig

    async def get_object(
        self, key: str, byte_range: tuple[int, int] | None = None
    ) -> AsyncGenerator[bytes, None]:
        inner = await super().get_object(key, byte_range)

        async def _tracked() -> AsyncGenerator[bytes, None]:
            try:
                async with aclosing(inner):
                    async for chunk in inner:
                        yield chunk
            finally:
                self._rig.events.append("close")

        body = _tracked()
        self._rig.bodies.append(body)
        return body


def _factory(rig: _Rig) -> S3ClientFactory:
    store = FakeS3Store()

    @asynccontextmanager
    async def _client() -> AsyncIterator[S3Client]:
        try:
            yield _TrackingClient(store, rig)
        finally:
            rig.events.append("client-exit")

    return _client


async def _close_early(stream: AsyncIterator[bytes]) -> None:
    await anext(stream)
    await stream.aclose()  # type: ignore[attr-defined]


def _fail_spool_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _full(self: _OffLoopFile, data: bytes) -> None:
        raise OSError(errno.ENOSPC, "injected: no space left on device")

    monkeypatch.setattr(_OffLoopFile, "write", _full)


# Each case stores the object, then returns the read that stops early.
_Case = Callable[
    [S3ClientFactory, pytest.MonkeyPatch], Awaitable[Callable[[], Awaitable[None]]]
]


async def _jar(
    factory: S3ClientFactory, _mp: pytest.MonkeyPatch
) -> Callable[[], Awaitable[None]]:
    storage = ObjectStorage(factory)
    key = await storage.put_jar(stream_of(_DATA))
    return lambda: _close_early(storage.open_jar(key))


async def _backup(
    factory: S3ClientFactory, _mp: pytest.MonkeyPatch
) -> Callable[[], Awaitable[None]]:
    storage = ObjectStorage(factory)
    community, server = new_scope()
    key = await storage.put_backup(community, server, stream_of(_DATA))
    return lambda: _close_early(storage.open_backup(community, server, key))


async def _archive_probe(
    factory: S3ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], Awaitable[None]]:
    storage = ObjectStorage(factory)
    community, server = new_scope()
    key = await storage.put_backup(community, server, stream_of(_DATA))
    _fail_spool_writes(monkeypatch)

    async def _act() -> None:
        with pytest.raises(OSError):
            await storage.check_backup_health(community, server, key)

    return _act


async def _restore_spool(
    factory: S3ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], Awaitable[None]]:
    storage = ObjectStorage(factory)
    community, server = new_scope()
    key = await storage.put_backup(community, server, stream_of(_DATA))
    _fail_spool_writes(monkeypatch)

    async def _act() -> None:
        with pytest.raises(OSError):
            await storage.restore_backup(community, server, key)

    return _act


async def _plugin_cache(
    factory: S3ClientFactory, _mp: pytest.MonkeyPatch
) -> Callable[[], Awaitable[None]]:
    cache = ObjectPluginCacheStore(factory)
    sha256 = hashlib.sha256(_DATA).hexdigest()
    await cache.put(sha256, stream_of(_DATA))
    return lambda: _close_early(cache.open(sha256))


async def _resource_pack(
    factory: S3ClientFactory, _mp: pytest.MonkeyPatch
) -> Callable[[], Awaitable[None]]:
    packs = ObjectResourcePackStore(factory)
    pack_id = ResourcePackId(uuid.uuid4())
    await packs.put(pack_id, "pack.zip", stream_of(_DATA))
    return lambda: _close_early(packs.open(pack_id, "pack.zip"))


@pytest.mark.parametrize(
    "case",
    [_jar, _backup, _archive_probe, _restore_spool, _plugin_cache, _resource_pack],
    ids=lambda case: case.__name__.lstrip("_"),
)
async def test_a_read_stopped_early_closes_the_body_before_its_client_exits(
    case: _Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _Rig()
    act = await case(_factory(rig), monkeypatch)
    rig.events.clear()

    await act()

    assert rig.events == ["close", "client-exit"]


async def test_stalling_forwards_aclose_to_the_body_it_wraps() -> None:
    closed: list[bool] = []

    async def _body() -> AsyncGenerator[bytes, None]:
        try:
            yield b"first"
            yield b"second"
        finally:
            closed.append(True)

    body = _body()
    stalling = _stalling(body, Faults())
    assert await anext(stalling) == b"first"

    await stalling.aclose()  # type: ignore[attr-defined]

    assert closed == [True]
