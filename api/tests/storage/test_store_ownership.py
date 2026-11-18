"""The store owner marker (STORAGE.md Section 4.3, issue #3870).

The GCs and the storage sweep delete what this database does not name, so they may
only run against a store that belongs to this database. At startup the API claims
its store: it writes an owner marker naming its installation id when none exists,
and keeps one that does. Before every destructive pass the marker is read again,
and the pass is refused unless the marker names this installation.

The same rules hold on both backends: a marker object in the bucket, and a marker
file at the fs root.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from mc_server_dashboard_api.storage.adapters.object_store import (
    ConditionalPutUnsupportedError,
    S3Client,
    S3ClientFactory,
)
from mc_server_dashboard_api.storage.adapters.ownership import (
    OWNER_MARKER,
    FsStoreOwnership,
    ObjectStoreOwnership,
)
from mc_server_dashboard_api.storage.domain.errors import NotFoundError
from tests.storage.fake_s3 import FakeS3Client, FakeS3Store, fake_s3_factory

_OURS = uuid.UUID("00000000-0000-4000-8000-000000000001")
_THEIRS = uuid.UUID("00000000-0000-4000-8000-000000000002")


def _marker(installation_id: uuid.UUID) -> bytes:
    return json.dumps(
        {
            "installation_id": str(installation_id),
            "created_at": "2026-01-01T00:00:00Z",
        }
    ).encode()


@dataclass
class _Backend:
    """One store under test: build an ownership over it, and read/seed its marker."""

    make: Callable[[], ObjectStoreOwnership | FsStoreOwnership]
    read: Callable[[], bytes | None]
    seed: Callable[[bytes], object]
    remove: Callable[[], object]


@pytest.fixture(params=["object", "fs"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> _Backend:
    if request.param == "object":
        store = FakeS3Store()
        return _Backend(
            make=lambda: ObjectStoreOwnership(fake_s3_factory(store), bucket="mcsd"),
            read=lambda: store.objects.get(OWNER_MARKER),
            seed=lambda body: store.objects.__setitem__(OWNER_MARKER, body),
            remove=lambda: store.objects.pop(OWNER_MARKER, None),
        )
    root = tmp_path / "storage"
    root.mkdir()
    path = root / OWNER_MARKER
    return _Backend(
        make=lambda: FsStoreOwnership(root),
        read=lambda: path.read_bytes() if path.exists() else None,
        seed=path.write_bytes,
        remove=lambda: path.unlink(missing_ok=True),
    )


def _named(body: bytes | None) -> str:
    assert body is not None
    named = json.loads(body)["installation_id"]
    assert isinstance(named, str)
    return named


async def test_claim_writes_a_missing_marker_naming_this_installation(
    backend: _Backend,
) -> None:
    ownership = backend.make()

    await ownership.claim(_OURS)

    assert _named(backend.read()) == str(_OURS)
    assert await ownership.refusal() is None


async def test_claim_keeps_a_marker_that_names_this_installation(
    backend: _Backend,
) -> None:
    backend.seed(_marker(_OURS))
    ownership = backend.make()

    await ownership.claim(_OURS)

    assert backend.read() == _marker(_OURS)
    assert await ownership.refusal() is None


async def test_a_marker_naming_another_installation_refuses_and_is_kept(
    backend: _Backend, caplog: pytest.LogCaptureFixture
) -> None:
    backend.seed(_marker(_THEIRS))
    ownership = backend.make()

    with caplog.at_level(logging.ERROR):
        await ownership.claim(_OURS)

    assert backend.read() == _marker(_THEIRS)
    refusal = await ownership.refusal()
    assert refusal is not None
    assert str(_THEIRS) in refusal
    assert str(_OURS) in refusal
    assert OWNER_MARKER in refusal
    assert any(
        rec.levelno == logging.ERROR and str(_THEIRS) in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b'{"installation_id": 7}', b'{"installation_id": "x"}'],
)
async def test_an_unreadable_marker_refuses_and_is_kept(
    backend: _Backend, body: bytes
) -> None:
    backend.seed(body)
    ownership = backend.make()

    await ownership.claim(_OURS)

    assert backend.read() == body
    assert await ownership.refusal() is not None


async def test_a_marker_removed_after_the_claim_refuses_rather_than_reclaims(
    backend: _Backend,
) -> None:
    # Handing a store to another database means removing its marker; a process
    # still running against it must stop, not take the store back.
    ownership = backend.make()
    await ownership.claim(_OURS)
    backend.remove()

    assert await ownership.refusal() is not None
    assert backend.read() is None


async def test_an_unclaimed_store_refuses(backend: _Backend) -> None:
    backend.seed(_marker(_OURS))

    assert await backend.make().refusal() is not None


async def test_a_marker_replaced_after_the_claim_refuses(backend: _Backend) -> None:
    ownership = backend.make()
    await ownership.claim(_OURS)
    backend.seed(_marker(_THEIRS))

    assert await ownership.refusal() is not None


async def test_an_fs_claim_creates_a_root_that_does_not_exist_yet(
    tmp_path: Path,
) -> None:
    root = tmp_path / "not-yet"
    ownership = FsStoreOwnership(root)

    await ownership.claim(_OURS)

    assert _named((root / OWNER_MARKER).read_bytes()) == str(_OURS)
    assert await ownership.refusal() is None


# --- concurrent first starts -------------------------------------------------


class _WaitAfterFirstMiss:
    """Delegate to a fake client; the first marker read that misses waits.

    The wait is a barrier both claimants reach after reading "no marker", so both
    go on to write it, which is the race two APIs starting together run. Every
    write that lands is recorded in ``landed``.
    """

    def __init__(
        self, inner: FakeS3Client, barrier: asyncio.Barrier, landed: list[bytes]
    ) -> None:
        self._inner = inner
        self._barrier = barrier
        self._landed = landed
        self._waited = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def get_object(
        self, key: str, byte_range: tuple[int, int] | None = None
    ) -> AsyncGenerator[bytes, None]:
        try:
            return await self._inner.get_object(key, byte_range)
        except NotFoundError:
            if not self._waited:
                self._waited = True
                await self._barrier.wait()
            raise

    async def put_object(self, key: str, body: bytes) -> None:
        await self._inner.put_object(key, body)
        self._landed.append(body)

    async def put_object_if_absent(self, key: str, body: bytes) -> bool:
        created = await self._inner.put_object_if_absent(key, body)
        if created:
            self._landed.append(body)
        return created


def _racing_factory(
    store: FakeS3Store, barrier: asyncio.Barrier, landed: list[bytes]
) -> S3ClientFactory:
    client = _WaitAfterFirstMiss(FakeS3Client(store), barrier, landed)

    @asynccontextmanager
    async def _factory() -> AsyncIterator[S3Client]:
        yield client

    return _factory


async def test_two_concurrent_first_starts_claim_the_bucket_once() -> None:
    store = FakeS3Store()
    barrier = asyncio.Barrier(2)
    landed: list[bytes] = []
    ours = ObjectStoreOwnership(_racing_factory(store, barrier, landed), bucket="b")
    theirs = ObjectStoreOwnership(_racing_factory(store, barrier, landed), bucket="b")

    await asyncio.gather(ours.claim(_OURS), theirs.claim(_THEIRS))

    # Both read "no marker" and both tried to write one, but only one write
    # landed: the second did not replace the first claimant's marker.
    assert landed == [store.objects[OWNER_MARKER]]
    owner = _named(store.objects[OWNER_MARKER])
    assert (await ours.refusal() is None) == (owner == str(_OURS))
    assert (await theirs.refusal() is None) == (owner == str(_THEIRS))


async def test_concurrent_first_starts_on_an_fs_root_claim_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    first = FsStoreOwnership(root)
    second = FsStoreOwnership(root)
    await first.claim(_OURS)
    # The second claimant read the root before the first wrote its marker.
    reads: list[Callable[[], bytes | None]] = [lambda: None]
    real_read = second._read_marker
    monkeypatch.setattr(
        second, "_read_marker", lambda: reads.pop()() if reads else real_read()
    )

    await second.claim(_THEIRS)

    assert _named((root / OWNER_MARKER).read_bytes()) == str(_OURS)
    assert await first.refusal() is None
    assert await second.refusal() is not None
    # The claim leaves no temporary file behind.
    assert sorted(p.name for p in root.iterdir()) == [OWNER_MARKER]


# --- a store without conditional writes -----------------------------------------


class _NoConditionalPut:
    """Delegate to a fake client whose conditional put is unsupported."""

    def __init__(self, inner: FakeS3Client) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def put_object_if_absent(self, key: str, body: bytes) -> bool:
        raise ConditionalPutUnsupportedError("stub: unsupported")


def _factory_of(client: object) -> S3ClientFactory:
    @asynccontextmanager
    async def _factory() -> AsyncIterator[S3Client]:
        yield client  # type: ignore[misc]

    return _factory


async def test_claim_falls_back_to_a_plain_put_and_reads_it_back() -> None:
    store = FakeS3Store()
    ownership = ObjectStoreOwnership(
        _factory_of(_NoConditionalPut(FakeS3Client(store))), bucket="mcsd"
    )

    await ownership.claim(_OURS)

    assert _named(store.objects[OWNER_MARKER]) == str(_OURS)
    assert await ownership.refusal() is None


async def test_the_marker_lies_outside_every_swept_namespace() -> None:
    from mc_server_dashboard_api.storage.adapters.object_store import KeyNamespace

    assert not any(OWNER_MARKER.startswith(prefix) for prefix in KeyNamespace)
