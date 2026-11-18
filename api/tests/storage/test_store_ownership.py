"""The store owner marker (STORAGE.md Section 4.3, issue #3870).

The GCs and the storage sweep delete what this database does not name, so they may
only run against a store that belongs to this database. At startup the API claims
its store: it keeps an owner marker that exists, and writes one naming its
installation id when none does, if the store is empty or the database has rows
that name stored objects (issue #3886). Before every destructive pass the marker
is read again, and the pass is refused unless the marker names this installation.
The operator's claim writes a marker whatever the store holds, but never replaces
one.

The same rules hold on both backends: a marker object in the bucket, and a marker
file at the fs root.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from mc_server_dashboard_api.storage.adapters import ownership as ownership_module
from mc_server_dashboard_api.storage.adapters.object_store import (
    ConditionalPutUnsupportedError,
    KeyNamespace,
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
_CLAIM_COMMAND = "python -m mc_server_dashboard_api.store_claim_cli"


def _marker(installation_id: uuid.UUID) -> bytes:
    return json.dumps(
        {
            "installation_id": str(installation_id),
            "created_at": "2026-01-01T00:00:00Z",
        }
    ).encode()


async def _rows() -> bool:
    """The database probe of a database with rows that name stored objects."""

    return True


async def _no_rows() -> bool:
    """The database probe of a fresh database: no row names a stored object."""

    return False


async def _never_asked() -> bool:
    raise AssertionError("probed a store that already has a marker")


@dataclass
class _Backend:
    """One store under test: build an ownership over it, and read/seed its marker.

    ``put`` stores one object under a namespace a GC or sweep lists, and ``held``
    says whether it is still there.
    """

    make: Callable[[], ObjectStoreOwnership | FsStoreOwnership]
    read: Callable[[], bytes | None]
    seed: Callable[[bytes], object]
    remove: Callable[[], object]
    put: Callable[[str], object]
    held: Callable[[str], bool]


def _backend(kind: str, tmp_path: Path) -> _Backend:
    if kind == "object":
        store = FakeS3Store()
        return _Backend(
            make=lambda: ObjectStoreOwnership(fake_s3_factory(store), bucket="mcsd"),
            read=lambda: store.objects.get(OWNER_MARKER),
            seed=lambda body: store.objects.__setitem__(OWNER_MARKER, body),
            remove=lambda: store.objects.pop(OWNER_MARKER, None),
            put=lambda ns: store.objects.__setitem__(f"{ns}held", b"live"),
            held=lambda ns: f"{ns}held" in store.objects,
        )
    root = tmp_path / "storage"
    root.mkdir()
    path = root / OWNER_MARKER

    def _put(namespace: str) -> None:
        (root / namespace).mkdir()
        (root / namespace / "held").write_bytes(b"live")

    return _Backend(
        make=lambda: FsStoreOwnership(root),
        read=lambda: path.read_bytes() if path.exists() else None,
        seed=path.write_bytes,
        remove=lambda: path.unlink(missing_ok=True),
        put=_put,
        held=lambda ns: (root / ns / "held").exists(),
    )


@pytest.fixture(params=["object", "fs"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> _Backend:
    return _backend(request.param, tmp_path)


# Every namespace a GC or sweep lists, on each backend: an object in any one of
# them makes the store hold objects.
_NAMESPACES = [("object", ns.value) for ns in KeyNamespace] + [
    ("fs", "communities"),
    ("fs", "jars"),
]


@pytest.fixture(params=_NAMESPACES, ids=lambda p: f"{p[0]}-{p[1].rstrip('/')}")
def holding(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[_Backend, str]:
    """An unmarked store holding one object, and the namespace it is under."""

    kind, namespace = request.param
    store = _backend(kind, tmp_path)
    store.put(namespace)
    return store, namespace


def _claim_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.ERROR and rec.name == ownership_module.__name__
    ]


def _named(body: bytes | None) -> str:
    assert body is not None
    named = json.loads(body)["installation_id"]
    assert isinstance(named, str)
    return named


async def test_an_empty_store_is_claimed_even_by_an_empty_database(
    backend: _Backend,
) -> None:
    # A fresh deployment: nothing in the store can be another database's.
    ownership = backend.make()

    await ownership.claim(_OURS, database_names_objects=_no_rows)

    assert _named(backend.read()) == str(_OURS)
    assert await ownership.refusal() is None


async def test_a_store_holding_objects_is_claimed_by_a_database_with_rows(
    holding: tuple[_Backend, str],
) -> None:
    # The upgrade of an existing deployment to the marker.
    backend, namespace = holding
    ownership = backend.make()

    await ownership.claim(_OURS, database_names_objects=_rows)

    assert _named(backend.read()) == str(_OURS)
    assert await ownership.refusal() is None
    assert backend.held(namespace)


async def test_a_store_holding_objects_is_not_claimed_by_an_empty_database(
    holding: tuple[_Backend, str], caplog: pytest.LogCaptureFixture
) -> None:
    # A probe or dev stack's fresh database on the live store (issue #3886).
    backend, namespace = holding
    ownership = backend.make()

    with caplog.at_level(logging.ERROR):
        await ownership.claim(_OURS, database_names_objects=_no_rows)

    assert backend.read() is None
    assert backend.held(namespace)
    refusal = await ownership.refusal()
    assert refusal is not None
    assert "has not claimed" in refusal
    assert _CLAIM_COMMAND in refusal
    (error,) = _claim_errors(caplog)
    assert OWNER_MARKER in error
    assert str(_OURS) in error
    assert _CLAIM_COMMAND in error


async def test_a_store_left_unclaimed_stays_refused_until_a_restart(
    holding: tuple[_Backend, str],
) -> None:
    # Only a start claims: the operator's claim takes effect at the next one.
    backend, _namespace = holding
    running = backend.make()
    await running.claim(_OURS, database_names_objects=_no_rows)

    assert await backend.make().claim_explicitly(_OURS) is None

    assert await running.refusal() is not None
    restarted = backend.make()
    await restarted.claim(_OURS, database_names_objects=_never_asked)
    assert await restarted.refusal() is None


@pytest.mark.parametrize("owner", [_OURS, _THEIRS])
async def test_a_marked_store_is_never_probed(
    backend: _Backend, monkeypatch: pytest.MonkeyPatch, owner: uuid.UUID
) -> None:
    backend.seed(_marker(owner))
    ownership = backend.make()
    monkeypatch.setattr(ownership, "_holds_objects", _never_asked)

    await ownership.claim(_OURS, database_names_objects=_never_asked)

    assert backend.read() == _marker(owner)


async def test_claim_keeps_a_marker_that_names_this_installation(
    backend: _Backend,
) -> None:
    backend.seed(_marker(_OURS))
    ownership = backend.make()

    await ownership.claim(_OURS, database_names_objects=_never_asked)

    assert backend.read() == _marker(_OURS)
    assert await ownership.refusal() is None


async def test_a_marker_naming_another_installation_refuses_and_is_kept(
    backend: _Backend, caplog: pytest.LogCaptureFixture
) -> None:
    backend.seed(_marker(_THEIRS))
    ownership = backend.make()

    with caplog.at_level(logging.ERROR):
        await ownership.claim(_OURS, database_names_objects=_never_asked)

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

    await ownership.claim(_OURS, database_names_objects=_never_asked)

    assert backend.read() == body
    assert await ownership.refusal() is not None


async def test_a_marker_removed_after_the_claim_refuses_rather_than_reclaims(
    backend: _Backend,
) -> None:
    # Handing a store to another database means removing its marker; a process
    # still running against it must stop, not take the store back.
    ownership = backend.make()
    await ownership.claim(_OURS, database_names_objects=_no_rows)
    backend.remove()

    assert await ownership.refusal() is not None
    assert backend.read() is None


async def test_an_unclaimed_store_refuses(backend: _Backend) -> None:
    backend.seed(_marker(_OURS))

    assert await backend.make().refusal() is not None


async def test_a_marker_replaced_after_the_claim_refuses(backend: _Backend) -> None:
    ownership = backend.make()
    await ownership.claim(_OURS, database_names_objects=_no_rows)
    backend.seed(_marker(_THEIRS))

    assert await ownership.refusal() is not None


async def test_an_fs_claim_creates_a_root_that_does_not_exist_yet(
    tmp_path: Path,
) -> None:
    root = tmp_path / "not-yet"
    ownership = FsStoreOwnership(root)

    await ownership.claim(_OURS, database_names_objects=_no_rows)

    assert _named((root / OWNER_MARKER).read_bytes()) == str(_OURS)
    assert await ownership.refusal() is None


async def test_an_fs_claim_removes_a_temp_file_a_crashed_claim_left(
    tmp_path: Path,
) -> None:
    # A crash between the temporary write and its removal leaves the temp file at
    # the root. The next claim removes it once it is old enough that no claim
    # still running can own it; a fresh one may be another API's, mid-claim.
    root = tmp_path / "storage"
    root.mkdir()
    stale = root / f".{OWNER_MARKER}.{'a' * 32}.tmp"
    fresh = root / f".{OWNER_MARKER}.{'b' * 32}.tmp"
    stale.write_bytes(b"{}")
    fresh.write_bytes(b"{}")
    two_hours_ago = time.time() - 7200
    os.utime(stale, (two_hours_ago, two_hours_ago))

    await FsStoreOwnership(root).claim(_OURS, database_names_objects=_no_rows)

    assert not stale.exists()
    assert fresh.exists()
    assert _named((root / OWNER_MARKER).read_bytes()) == str(_OURS)


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

    await asyncio.gather(
        ours.claim(_OURS, database_names_objects=_no_rows),
        theirs.claim(_THEIRS, database_names_objects=_no_rows),
    )

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
    await first.claim(_OURS, database_names_objects=_no_rows)
    _miss_first_read(second, monkeypatch)

    await second.claim(_THEIRS, database_names_objects=_no_rows)

    assert _named((root / OWNER_MARKER).read_bytes()) == str(_OURS)
    assert await first.refusal() is None
    assert await second.refusal() is not None
    # The claim leaves no temporary file behind.
    assert sorted(p.name for p in root.iterdir()) == [OWNER_MARKER]


def _miss_first_read(
    ownership: FsStoreOwnership, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claimant read the root before another wrote its marker."""

    reads: list[Callable[[], bytes | None]] = [lambda: None]
    real_read = ownership._read_marker
    monkeypatch.setattr(
        ownership, "_read_marker", lambda: reads.pop()() if reads else real_read()
    )


async def test_an_operator_claim_that_loses_the_race_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    (root / OWNER_MARKER).write_bytes(_marker(_THEIRS))
    ownership = FsStoreOwnership(root)
    _miss_first_read(ownership, monkeypatch)

    refusal = await ownership.claim_explicitly(_OURS)

    assert refusal is not None
    assert str(_THEIRS) in refusal
    assert (root / OWNER_MARKER).read_bytes() == _marker(_THEIRS)


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

    await ownership.claim(_OURS, database_names_objects=_no_rows)

    assert _named(store.objects[OWNER_MARKER]) == str(_OURS)
    assert await ownership.refusal() is None


async def test_the_plain_put_fallback_never_replaces_another_installations_marker() -> (
    None
):
    # Without a conditional write, only the read before the put keeps a claim from
    # taking over a store another database already owns.
    store = FakeS3Store()
    store.objects[OWNER_MARKER] = _marker(_THEIRS)
    ownership = ObjectStoreOwnership(
        _factory_of(_NoConditionalPut(FakeS3Client(store))), bucket="mcsd"
    )

    await ownership.claim(_OURS, database_names_objects=_never_asked)

    assert store.objects[OWNER_MARKER] == _marker(_THEIRS)
    assert await ownership.refusal() is not None


async def test_the_plain_put_fallback_keeps_a_marker_from_an_operator_claim() -> None:
    store = FakeS3Store()
    store.objects[OWNER_MARKER] = _marker(_THEIRS)
    ownership = ObjectStoreOwnership(
        _factory_of(_NoConditionalPut(FakeS3Client(store))), bucket="mcsd"
    )

    assert await ownership.claim_explicitly(_OURS) is not None
    assert store.objects[OWNER_MARKER] == _marker(_THEIRS)


async def test_the_marker_lies_outside_every_swept_namespace() -> None:
    assert not any(OWNER_MARKER.startswith(prefix) for prefix in KeyNamespace)
