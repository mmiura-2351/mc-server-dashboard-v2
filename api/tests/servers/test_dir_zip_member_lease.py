"""A dir zip closed mid-member closes the member under its own lease (#3734).

Closing the download exits the working-set view, releasing the view's lease,
before the member walk is closed and with it the member's stream. That stream
holds a lease of its own (issues #3618, #3670), so its body is still closed with
its snapshot leased, on both backends, through the servers seam.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from mc_server_dashboard_api.servers.adapters.file_store import (
    StorageFileStoreAdapter,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)
from mc_server_dashboard_api.storage.adapters import fs
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import (
    ObjectStorage,
    S3Client,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.storage.fake_s3 import FakeS3Client, FakeS3Store
from tests.storage.helpers import tar_stream

# The only member, so the zip's first chunk leaves it open.
_MEMBER = "level.dat"
_DATA = b"0123456789abcdef"

_Storage = FsStorage | ObjectStorage
_Log = list[tuple[str, int]]


def _held(storage: _Storage) -> int:
    with storage._lease_lock:
        return sum(storage._leases.values())


class _WatchedHandle:
    """A real file whose close is logged once it has finished."""

    def __init__(self, handle: io.BufferedReader, on_close: Callable[[], None]) -> None:
        self.name = handle.name
        self.read = handle.read
        self._handle = handle
        self._on_close = on_close

    def close(self) -> None:
        self._handle.close()
        self._on_close()


def _fs_storage(
    tmp_path: Path, on_close: Callable[[], None], monkeypatch: pytest.MonkeyPatch
) -> _Storage:
    open_readable = fs._open_readable_sync

    def _opening(path: Path, not_found: str) -> Any:
        handle = open_readable(path, not_found)
        if path.name != _MEMBER:
            return handle
        return _WatchedHandle(handle, on_close)

    monkeypatch.setattr(fs, "_open_readable_sync", _opening)
    return FsStorage(tmp_path)


class _WatchedClient(FakeS3Client):
    """Serves the member's body through a generator whose close is logged."""

    def __init__(self, store: FakeS3Store, on_close: Callable[[], None]) -> None:
        super().__init__(store)
        self._on_close = on_close

    async def get_object(
        self, key: str, byte_range: tuple[int, int] | None = None
    ) -> AsyncGenerator[bytes, None]:
        body = await super().get_object(key, byte_range)
        if not key.endswith("/" + _MEMBER):
            return body
        on_close = self._on_close

        async def _watched() -> AsyncGenerator[bytes, None]:
            try:
                async with aclosing(body):
                    async for chunk in body:
                        yield chunk
            finally:
                on_close()

        return _watched()


def _object_storage(
    tmp_path: Path, on_close: Callable[[], None], monkeypatch: pytest.MonkeyPatch
) -> _Storage:
    store = FakeS3Store()

    @asynccontextmanager
    async def _factory() -> AsyncIterator[S3Client]:
        yield _WatchedClient(store, on_close)

    return ObjectStorage(_factory)


_Build = Callable[[Path, Callable[[], None], pytest.MonkeyPatch], _Storage]


@pytest.mark.parametrize("build", [_fs_storage, _object_storage], ids=["fs", "object"])
async def test_a_dir_zip_closed_mid_member_closes_its_body_under_its_own_lease(
    build: _Build, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: _Log = []

    def _note(event: str) -> None:
        log.append((event, _held(storage)))

    storage = build(tmp_path, lambda: _note("body-close"), monkeypatch)
    release = storage._release_lease

    def _releasing(prefix: Any) -> None:
        release(prefix)
        _note("release")

    monkeypatch.setattr(storage, "_release_lease", _releasing)

    community, server = uuid.uuid4(), uuid.uuid4()
    scope = (StorageCommunityId(community), StorageServerId(server))
    handle = await storage.begin_snapshot(*scope)
    await storage.write_snapshot(handle, tar_stream({"world/" + _MEMBER: _DATA}))
    await storage.commit_snapshot(handle)

    seam = StorageFileStoreAdapter(storage=storage)
    stream = seam.download_dir(
        community_id=CommunityId(community),
        server_id=ServerId(server),
        rel_path="world",
    )
    # The member's header is out and its first chunk read; its body is open.
    await anext(stream)
    assert "body-close" not in [event for event, _ in log]
    begun = len(log)

    await stream.aclose()  # type: ignore[attr-defined]

    # The view's release leaves one lease, the member stream's own; its body
    # closes under it, and only then is it released.
    assert log[begun:] == [("release", 1), ("body-close", 1), ("release", 0)]
    assert _held(storage) == 0
