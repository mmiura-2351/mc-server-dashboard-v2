"""Object stream leases: the view stream's own lease and the hydrate swap (#3670).

A working-set view stream takes a lease of its own on its first iteration, as the
fs one does (issue #3618), so a stream left open past its view's exit still pins
its prefix. The hydrate stream re-leases when a publish lands between its first
lease and its generation read; each prefix is leased and released exactly once.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

import pytest

from mc_server_dashboard_api.storage.adapters.object_store import (
    ObjectStorage,
    S3Client,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    RelPath,
    ServerId,
)
from tests.storage.fake_s3 import FakeS3Client, FakeS3Store, fake_s3_factory
from tests.storage.helpers import new_scope, tar_stream

_NAME = "f.txt"


class _StallingClient(FakeS3Client):
    """Serves the watched object's first byte, then parks on the next read."""

    def __init__(self, store: FakeS3Store, stalled: asyncio.Event) -> None:
        super().__init__(store)
        self._stalled = stalled

    async def get_object(
        self, key: str, byte_range: tuple[int, int] | None = None
    ) -> AsyncGenerator[bytes, None]:
        if not key.endswith("/" + _NAME):
            return await super().get_object(key, byte_range)
        data = self._store.objects[key]
        stalled = self._stalled

        async def _gen() -> AsyncGenerator[bytes, None]:
            yield data[:1]
            stalled.set()
            await asyncio.Event().wait()

        return _gen()


async def _publish(
    storage: ObjectStorage, community: CommunityId, server: ServerId, data: bytes
) -> None:
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream({_NAME: data}))
    await storage.commit_snapshot(handle)


async def _pointer(
    storage: ObjectStorage, community: CommunityId, server: ServerId
) -> str | None:
    async with storage._client_factory() as client:
        return await storage._read_pointer(
            client, storage._server_prefix(community, server)
        )


async def test_a_view_stream_never_iterated_takes_no_lease() -> None:
    storage = ObjectStorage(fake_s3_factory(FakeS3Store()))
    community, server = new_scope()
    await _publish(storage, community, server, b"DATA")

    async with storage.open_working_set_view(community, server) as view:
        view.open_file_stream(RelPath(_NAME))
        # The view's lease only: the stream leases on its first iteration.
        assert sum(storage._leases.values()) == 1

    assert not storage._leases


async def test_a_view_stream_cancelled_past_its_view_releases_its_lease() -> None:
    store = FakeS3Store()
    stalled = asyncio.Event()

    @asynccontextmanager
    async def _factory() -> AsyncIterator[S3Client]:
        yield _StallingClient(store, stalled)

    storage = ObjectStorage(_factory)
    community, server = new_scope()
    await _publish(storage, community, server, b"DATA")

    async with storage.open_working_set_view(community, server) as view:
        stream = view.open_file_stream(RelPath(_NAME))
        assert await stream.__anext__() == b"D"

    async def _next() -> bytes:
        return await stream.__anext__()

    task = asyncio.ensure_future(_next())
    await asyncio.wait_for(stalled.wait(), 10)
    # The view is gone; the parked stream still holds its own lease.
    assert sum(storage._leases.values()) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not storage._leases


async def test_a_hydrate_releases_once_each_prefix_it_swapped_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ObjectStorage(fake_s3_factory(FakeS3Store()))
    community, server = new_scope()
    await _publish(storage, community, server, b"OLD")
    old = await _pointer(storage, community, server)

    ledger: list[tuple[str, str]] = []
    acquire = storage._acquire_lease
    release = storage._release_lease

    def _acquiring(prefix: str) -> None:
        ledger.append(("+", prefix))
        acquire(prefix)

    def _releasing(prefix: str) -> None:
        ledger.append(("-", prefix))
        release(prefix)

    monkeypatch.setattr(storage, "_acquire_lease", _acquiring)
    monkeypatch.setattr(storage, "_release_lease", _releasing)

    lease = storage._lease_live_snapshot
    published: list[bool] = []

    async def _lease_then_publish(
        client: S3Client, community_id: CommunityId, server_id: ServerId
    ) -> str:
        prefix = await lease(client, community_id, server_id)
        if not published:
            # A publish lands between the first lease and the generation read.
            published.append(True)
            await _publish(storage, community, server, b"NEW")
        return prefix

    monkeypatch.setattr(storage, "_lease_live_snapshot", _lease_then_publish)

    source = storage.open_hydrate_source(community, server)
    await source.__anext__()
    await source.aclose()

    new = await _pointer(storage, community, server)
    assert old is not None and new is not None and old != new
    assert ledger == [("+", old), ("+", new), ("-", old), ("-", new)]
    assert not storage._leases
