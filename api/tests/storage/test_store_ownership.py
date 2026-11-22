"""The store owner marker (STORAGE.md Section 4.3, issue #3870).

The GCs and the storage sweep delete what this database does not name, so they may
only run against a store that belongs to this database. At startup the API claims
its store: it keeps an owner marker that exists, and writes one naming its
installation id when none does, only on evidence that the store is this
database's (issue #3886). An empty store is. A store that holds objects is if
the communities and resource packs its keys name, a few sampled per namespace,
are all rows of this database; one holding only JARs and plugin blobs is if the
database has rows that name stored objects. Before every destructive pass the
marker is read again, and the pass is refused unless the marker names this
installation. The operator's claim writes a marker whatever the store holds, but
never replaces one.

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
from tests.store_ownership import FakeStoreDatabase, UnaskedStoreDatabase

_OURS = uuid.UUID("00000000-0000-4000-8000-000000000001")
_THEIRS = uuid.UUID("00000000-0000-4000-8000-000000000002")
_CLAIM_COMMAND = "python -m mc_server_dashboard_api.store_claim_cli"

# Communities and a resource pack, as their ids appear in keys. ``_PROBE`` is
# a probe database's own community, which no store under test holds.
_LIVE = uuid.UUID("10000000-0000-4000-8000-000000000001")
_OTHER = uuid.UUID("20000000-0000-4000-8000-000000000002")
_PROBE = uuid.UUID("30000000-0000-4000-8000-000000000003")
_PACK = uuid.UUID("40000000-0000-4000-8000-000000000004")

_UNASKED = UnaskedStoreDatabase()


def _marker(installation_id: uuid.UUID) -> bytes:
    return json.dumps(
        {
            "installation_id": str(installation_id),
            "created_at": "2026-01-01T00:00:00Z",
        }
    ).encode()


def _world(community: uuid.UUID | str, server: str = "s") -> str:
    """A key of a community's server, as the storage adapters lay it out."""

    return f"communities/{community}/servers/{server}/snapshots/x/world/level.dat"


_JAR = f"jars/{'a' * 64}.jar"
_PLUGIN = f"plugin-cache/{'b' * 64}"
_PACK_KEY = f"resource-packs/{_PACK}/pack.zip"


@dataclass
class _Backend:
    """One store under test: build an ownership over it, and read/seed its marker.

    ``put`` stores one object at a key (its path under the fs root), and ``held``
    says whether it is still there.
    """

    kind: str
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
            kind=kind,
            make=lambda: ObjectStoreOwnership(fake_s3_factory(store), bucket="mcsd"),
            read=lambda: store.objects.get(OWNER_MARKER),
            seed=lambda body: store.objects.__setitem__(OWNER_MARKER, body),
            remove=lambda: store.objects.pop(OWNER_MARKER, None),
            put=lambda key: store.objects.__setitem__(key, b"live"),
            held=lambda key: key in store.objects,
        )
    root = tmp_path / "storage"
    root.mkdir()
    path = root / OWNER_MARKER

    def _put(key: str) -> None:
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        (root / key).write_bytes(b"live")

    return _Backend(
        kind=kind,
        make=lambda: FsStoreOwnership(root),
        read=lambda: path.read_bytes() if path.exists() else None,
        seed=path.write_bytes,
        remove=lambda: path.unlink(missing_ok=True),
        put=_put,
        held=lambda key: (root / key).exists(),
    )


@pytest.fixture(params=["object", "fs"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> _Backend:
    return _backend(request.param, tmp_path)


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


async def _assert_claimed(backend: _Backend, database: FakeStoreDatabase) -> None:
    ownership = backend.make()

    await ownership.claim(_OURS, database=database)

    assert _named(backend.read()) == str(_OURS)
    assert await ownership.refusal() is None


async def _assert_refused(
    backend: _Backend,
    database: FakeStoreDatabase,
    caplog: pytest.LogCaptureFixture,
) -> str:
    """Claim, expect no marker and one ERROR naming the step; return the ERROR."""

    ownership = backend.make()

    with caplog.at_level(logging.ERROR):
        await ownership.claim(_OURS, database=database)

    assert backend.read() is None
    refusal = await ownership.refusal()
    assert refusal is not None
    assert "has not claimed" in refusal
    assert _CLAIM_COMMAND in refusal
    (error,) = _claim_errors(caplog)
    assert OWNER_MARKER in error
    assert str(_OURS) in error
    assert _CLAIM_COMMAND in error
    return error


# --- a store without a marker -----------------------------------------------------


async def test_an_empty_store_is_claimed_without_asking_the_database(
    backend: _Backend,
) -> None:
    # A fresh deployment: nothing in the store can be another database's.
    ownership = backend.make()

    await ownership.claim(_OURS, database=_UNASKED)

    assert _named(backend.read()) == str(_OURS)
    assert await ownership.refusal() is None


async def test_a_store_of_this_databases_community_is_claimed(
    backend: _Backend,
) -> None:
    # The upgrade of an existing deployment: its keys name its own community.
    # The id is the evidence, so the rows rule is not asked.
    backend.put(_world(_LIVE))
    backend.put(_JAR)
    database = FakeStoreDatabase(communities=[_LIVE])

    await _assert_claimed(backend, database)

    assert backend.held(_world(_LIVE))
    assert database.asked == [("has", str(_LIVE))]


async def test_a_probe_database_with_its_own_community_does_not_claim_another(
    backend: _Backend, caplog: pytest.LogCaptureFixture
) -> None:
    # A probe or dev stack on the live store, past its first community and server:
    # it has rows, but none of the communities the store holds (issue #3886).
    backend.put(_world(_LIVE))
    database = FakeStoreDatabase(rows=True, communities=[_PROBE])

    error = await _assert_refused(backend, database, caplog)

    assert str(_LIVE) in error
    assert backend.held(_world(_LIVE))


async def test_one_unknown_community_among_known_ones_refuses(
    backend: _Backend, caplog: pytest.LogCaptureFixture
) -> None:
    # A community this database has sorts first and holds many keys: the sample
    # reaches past them to the next community, which this database lacks.
    for server in ("s1", "s2", "s3", "s4"):
        backend.put(_world(_LIVE, server))
    backend.put(_world(_OTHER))
    database = FakeStoreDatabase(rows=True, communities=[_LIVE])

    await _assert_refused(backend, database, caplog)


@pytest.mark.parametrize("name", ["c", "not-a-uuid", f"{_LIVE}x"])
async def test_a_key_that_names_no_community_refuses(
    backend: _Backend, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    backend.put(_world(name))
    database = FakeStoreDatabase(rows=True, communities=[_LIVE])

    await _assert_refused(backend, database, caplog)


async def test_the_claim_samples_at_most_three_communities(
    backend: _Backend,
) -> None:
    communities = [
        uuid.UUID(f"{i}0000000-0000-4000-8000-00000000000{i}") for i in range(1, 6)
    ]
    for community in communities:
        backend.put(_world(community))
    database = FakeStoreDatabase(communities=communities)

    await _assert_claimed(backend, database)

    ((question, *asked),) = database.asked
    assert question == "has"
    assert len(asked) == 3
    assert set(asked) <= {str(c) for c in communities}


async def test_a_store_of_this_databases_resource_pack_is_claimed() -> None:
    # Resource packs exist on the object backend only.
    backend = _backend("object", Path())
    backend.put(_PACK_KEY)

    await _assert_claimed(backend, FakeStoreDatabase(resource_packs=[_PACK]))


async def test_a_resource_pack_this_database_lacks_refuses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = _backend("object", Path())
    backend.put(_PACK_KEY)
    backend.put(_world(_LIVE))
    database = FakeStoreDatabase(rows=True, communities=[_LIVE])

    error = await _assert_refused(backend, database, caplog)

    assert str(_PACK) in error


async def test_a_resource_pack_key_that_names_no_pack_refuses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Beside a community this database has, one segment that is not an id is
    # enough to refuse.
    backend = _backend("object", Path())
    backend.put(_world(_LIVE))
    backend.put("resource-packs/not-an-id/pack.zip")
    database = FakeStoreDatabase(rows=True, communities=[_LIVE])

    error = await _assert_refused(backend, database, caplog)

    assert "not-an-id" in error


def test_every_key_namespace_is_classified_for_the_claim() -> None:
    # A namespace added to KeyNamespace fails here until the claim is told how to
    # read it; until then, a store holding it refuses the claim (next test).
    assert ownership_module._ID_NAMESPACES == (
        KeyNamespace.COMMUNITIES,
        KeyNamespace.RESOURCE_PACKS,
    )
    assert ownership_module._CONTENT_NAMESPACES == (
        KeyNamespace.JARS,
        KeyNamespace.PLUGIN_CACHE,
    )
    assert {
        *ownership_module._ID_NAMESPACES,
        *ownership_module._CONTENT_NAMESPACES,
    } == set(KeyNamespace)


@pytest.mark.parametrize("with_ids", [False, True], ids=["alone", "beside-ids"])
async def test_a_namespace_the_claim_does_not_classify_refuses(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, with_ids: bool
) -> None:
    # plugin-cache/, declassified, stands in for a namespace added later: neither
    # the rows rule nor matching ids beside it claim the store.
    monkeypatch.setattr(ownership_module, "_CONTENT_NAMESPACES", (KeyNamespace.JARS,))
    backend = _backend("object", Path())
    backend.put(_PLUGIN)
    if with_ids:
        backend.put(_world(_LIVE))
    database = FakeStoreDatabase(rows=True, communities=[_LIVE])

    error = await _assert_refused(backend, database, caplog)

    assert "plugin-cache/" in error


class _ListingRecorder(FakeS3Client):
    """A fake client that records the prefix of every ``first_key`` listing."""

    def __init__(self, store: FakeS3Store, listed: list[str]) -> None:
        super().__init__(store)
        self._listed = listed

    async def first_key(
        self, prefix: str, *, start_after: str | None = None
    ) -> str | None:
        self._listed.append(prefix)
        return await super().first_key(prefix, start_after=start_after)


async def test_ids_decide_without_listing_the_content_namespaces() -> None:
    store = FakeS3Store()
    store.objects[_world(_LIVE)] = b"live"
    store.objects[_JAR] = b"jar"
    listed: list[str] = []
    ownership = ObjectStoreOwnership(
        _factory_of(_ListingRecorder(store, listed)), bucket="mcsd"
    )

    await ownership.claim(_OURS, database=FakeStoreDatabase(communities=[_LIVE]))

    assert await ownership.refusal() is None
    assert set(listed) == {"communities/", "resource-packs/"}


async def test_ids_decide_without_reading_jars_on_fs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _backend("fs", tmp_path)
    backend.put(_world(_LIVE))
    backend.put(_JAR)
    ownership = backend.make()
    assert isinstance(ownership, FsStoreOwnership)
    read: list[str] = []
    entries = ownership._entries

    def _recording(name: str) -> tuple[str, ...]:
        read.append(name)
        return entries(name)

    monkeypatch.setattr(ownership, "_entries", _recording)

    await ownership.claim(_OURS, database=FakeStoreDatabase(communities=[_LIVE]))

    assert await ownership.refusal() is None
    assert read == ["communities"]


# The content-addressed namespaces on each backend: a key there names no row.
_CONTENT = [("object", _JAR), ("object", _PLUGIN), ("fs", _JAR)]


@pytest.fixture(params=_CONTENT, ids=lambda p: f"{p[0]}-{p[1].split('/')[0]}")
def content(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[_Backend, str]:
    """An unmarked store holding one content-addressed object, and its key."""

    kind, key = request.param
    store = _backend(kind, tmp_path)
    store.put(key)
    return store, key


async def test_a_store_of_blobs_only_is_claimed_by_a_database_with_rows(
    content: tuple[_Backend, str],
) -> None:
    backend, key = content

    await _assert_claimed(backend, FakeStoreDatabase(rows=True))

    assert backend.held(key)


async def test_a_store_of_blobs_only_is_not_claimed_by_a_fresh_database(
    content: tuple[_Backend, str], caplog: pytest.LogCaptureFixture
) -> None:
    backend, key = content

    await _assert_refused(backend, FakeStoreDatabase(), caplog)

    assert backend.held(key)


async def test_a_store_left_unclaimed_stays_refused_until_a_restart(
    backend: _Backend,
) -> None:
    # Only a start claims: the operator's claim takes effect at the next one.
    backend.put(_world(_LIVE))
    running = backend.make()
    await running.claim(_OURS, database=FakeStoreDatabase())

    assert await backend.make().claim_explicitly(_OURS) is None

    assert await running.refusal() is not None
    restarted = backend.make()
    await restarted.claim(_OURS, database=_UNASKED)
    assert await restarted.refusal() is None


async def _never_sampled() -> object:
    raise AssertionError("sampled a store that already has a marker")


@pytest.mark.parametrize("owner", [_OURS, _THEIRS])
async def test_a_marked_store_is_never_sampled(
    backend: _Backend, monkeypatch: pytest.MonkeyPatch, owner: uuid.UUID
) -> None:
    backend.seed(_marker(owner))
    ownership = backend.make()
    monkeypatch.setattr(ownership, "_sample", _never_sampled)

    await ownership.claim(_OURS, database=_UNASKED)

    assert backend.read() == _marker(owner)


async def test_claim_keeps_a_marker_that_names_this_installation(
    backend: _Backend,
) -> None:
    backend.seed(_marker(_OURS))
    ownership = backend.make()

    await ownership.claim(_OURS, database=_UNASKED)

    assert backend.read() == _marker(_OURS)
    assert await ownership.refusal() is None


async def test_a_marker_naming_another_installation_refuses_and_is_kept(
    backend: _Backend, caplog: pytest.LogCaptureFixture
) -> None:
    backend.seed(_marker(_THEIRS))
    ownership = backend.make()

    with caplog.at_level(logging.ERROR):
        await ownership.claim(_OURS, database=_UNASKED)

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

    await ownership.claim(_OURS, database=_UNASKED)

    assert backend.read() == body
    assert await ownership.refusal() is not None


async def test_a_marker_removed_after_the_claim_refuses_rather_than_reclaims(
    backend: _Backend,
) -> None:
    # Handing a store to another database means removing its marker; a process
    # still running against it must stop, not take the store back.
    ownership = backend.make()
    await ownership.claim(_OURS, database=FakeStoreDatabase())
    backend.remove()

    assert await ownership.refusal() is not None
    assert backend.read() is None


async def test_an_unclaimed_store_refuses(backend: _Backend) -> None:
    backend.seed(_marker(_OURS))

    assert await backend.make().refusal() is not None


async def test_a_marker_replaced_after_the_claim_refuses(backend: _Backend) -> None:
    ownership = backend.make()
    await ownership.claim(_OURS, database=FakeStoreDatabase())
    backend.seed(_marker(_THEIRS))

    assert await ownership.refusal() is not None


# --- an operator command that claims nothing (issue #4107) ----------------------


async def test_a_command_may_run_on_a_store_whose_marker_names_its_installation(
    backend: _Backend,
) -> None:
    # An operator command does not claim the store; it asks whether the marker
    # names the installation of the database it was pointed at.
    backend.seed(_marker(_OURS))

    assert await backend.make().refusal_for(_OURS) is None


@pytest.mark.parametrize(
    "body",
    [None, _marker(_THEIRS), b"not json", b'{"installation_id": "x"}'],
    ids=["no-marker", "another-installation", "not-json", "no-installation"],
)
async def test_a_command_is_refused_on_a_store_not_marked_for_its_installation(
    backend: _Backend, body: bytes | None
) -> None:
    if body is not None:
        backend.seed(body)

    refusal = await backend.make().refusal_for(_OURS)

    assert refusal is not None
    assert OWNER_MARKER in refusal
    # Asking claims nothing: the store's marker is left as it was.
    assert backend.read() == body


async def test_an_fs_claim_creates_a_root_that_does_not_exist_yet(
    tmp_path: Path,
) -> None:
    root = tmp_path / "not-yet"
    ownership = FsStoreOwnership(root)

    await ownership.claim(_OURS, database=FakeStoreDatabase())

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

    await FsStoreOwnership(root).claim(_OURS, database=FakeStoreDatabase())

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
        ours.claim(_OURS, database=FakeStoreDatabase()),
        theirs.claim(_THEIRS, database=FakeStoreDatabase()),
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
    await first.claim(_OURS, database=FakeStoreDatabase())
    _miss_first_read(second, monkeypatch)

    await second.claim(_THEIRS, database=FakeStoreDatabase())

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

    await ownership.claim(_OURS, database=FakeStoreDatabase())

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

    await ownership.claim(_OURS, database=_UNASKED)

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


class _ReplacedAfterPut(_NoConditionalPut):
    """A plain write that another claimant's plain write replaces at once."""

    async def put_object(self, key: str, body: bytes) -> None:
        await self._inner.put_object(key, body)
        await self._inner.put_object(key, _marker(_THEIRS))


async def test_an_operator_claim_without_conditional_writes_reads_its_marker_back() -> (
    None
):
    # The read before the put cannot see a marker written after it; the read after
    # tells the operator that another claimant's write landed last.
    store = FakeS3Store()
    ownership = ObjectStoreOwnership(
        _factory_of(_ReplacedAfterPut(FakeS3Client(store))), bucket="mcsd"
    )

    refusal = await ownership.claim_explicitly(_OURS)

    assert refusal is not None
    assert str(_THEIRS) in refusal


class _GoneAfterPut(FakeS3Client):
    """A conditional write that reports success, and a marker gone at once."""

    async def put_object_if_absent(self, key: str, body: bytes) -> bool:
        return True


async def test_an_operator_claim_that_reads_no_marker_back_says_so() -> None:
    store = FakeS3Store()
    ownership = ObjectStoreOwnership(_factory_of(_GoneAfterPut(store)), bucket="mcsd")

    refusal = await ownership.claim_explicitly(_OURS)

    assert refusal is not None
    assert "has no owner marker" in refusal
    assert "names no installation" not in refusal


# --- why the operator's claim did not claim a marked store -------------------------


@pytest.mark.parametrize(
    ("body", "says"),
    [
        (_marker(_OURS), "already this database's: its owner marker"),
        (_marker(_THEIRS), f"names installation {_THEIRS}, not this database's"),
        (b"not json", "names no installation"),
    ],
    ids=["ours", "theirs", "unreadable"],
)
async def test_an_operator_claim_says_why_a_marked_store_was_not_claimed(
    backend: _Backend, body: bytes, says: str
) -> None:
    backend.seed(body)

    refusal = await backend.make().claim_explicitly(_OURS)

    assert refusal is not None
    assert says in refusal
    assert "None" not in refusal
    assert backend.read() == body
