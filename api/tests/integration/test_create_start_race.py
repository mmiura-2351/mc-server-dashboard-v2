"""A start racing a server's creation never hydrates a half-written working set.

``CreateServer`` commits the new row and then seeds the server's working set, and
``ImportServer`` then publishes the archive into it. Each write publishes on its
own, so a start admitted in between hydrated the tree as far as it had got, and
the rest of the writes landed under a running server (issue #4045). The creation
now holds the server's lifecycle lock from before its row is visible until its
last write, so a start racing it either waits for the whole tree or, once its
acquire budget runs out, is refused ``server_busy``.

These run the real use cases over a real PostgreSQL advisory lock and the fs
storage adapter. The creator's file store parks before one write, and the
control plane records what the store serves when the start hydrates: what a
Worker would unpack.

A creation that fails after its row commits leaves the row and what was
written. The row says its creation did not finish until the creation ends it,
so a start after the failure is refused and hydrates nothing (issue #4065).

DB-gated (TESTING.md Section 5): runs only when ``MCD_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import os
import tarfile
import uuid
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId as CommunityCommunityId,
)
from mc_server_dashboard_api.community.domain.value_objects import CommunityName
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.fleet.adapters.real_time_events import (
    InProcessRealTimeEvents,
)
from mc_server_dashboard_api.servers.adapters import (
    lifecycle_lock as lifecycle_lock_mod,
)
from mc_server_dashboard_api.servers.adapters.file_store import StorageFileStoreAdapter
from mc_server_dashboard_api.servers.adapters.lifecycle_lock import PgLifecycleLock
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.export_import import (
    EXPORT_FORMAT_VERSION,
    EXPORT_METADATA_FILENAME,
    ImportServer,
)
from mc_server_dashboard_api.servers.application.lifecycle import StartServer
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.control_plane import CommandOutcome
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    ServerBusyError,
    ServerCreationIncompleteError,
    WorkingSetSeedFailedError,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ServerId,
    WorkerId,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.port import Storage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.core.fakes import RecordingSleep
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import (
    FakeControlPlane,
    FakeJarProvisioner,
    FakeStoreGenerationReader,
    FakeVersionValidator,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
    ),
    # Every publish and edit on the fs backend fsyncs (issue #3651).
    pytest.mark.timeout(300),
]

_NOW = dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.timezone.utc)

# The import's archive. eula.txt comes first, so a start that reads the EULA
# from the store finds it accepted while the rest is still being written.
_ARCHIVE_MEMBERS = {
    "eula.txt": b"eula=true\n",
    "world/level.dat": b"level",
    "world/region/r.0.0.mca": b"region",
    "config/notes.txt": b"notes",
}


class _FixedClock(Clock):
    def now(self) -> dt.datetime:
        return _NOW

    def monotonic(self) -> float:
        return 0.0


class _ParkingFileStore(StorageFileStoreAdapter):
    """Parks the creator before its write of ``park_at``, until released."""

    def __init__(self, *, storage: Storage, park_at: str) -> None:
        super().__init__(storage=storage)
        self._park_at = park_at
        self.parked = asyncio.Event()
        self.release = asyncio.Event()
        self.server_id: ServerId | None = None

    async def write_file(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        content: bytes,
    ) -> None:
        if rel_path == self._park_at and not self.parked.is_set():
            self.server_id = server_id
            self.parked.set()
            await self.release.wait()
        await super().write_file(
            community_id=community_id,
            server_id=server_id,
            rel_path=rel_path,
            content=content,
        )


class _FailingFileStore(StorageFileStoreAdapter):
    """Fails the creator's write of ``fail_at``, as a storage outage would."""

    def __init__(self, *, storage: Storage, fail_at: str) -> None:
        super().__init__(storage=storage)
        self._fail_at = fail_at

    async def write_file(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        content: bytes,
    ) -> None:
        if rel_path == self._fail_at:
            raise OSError("forced storage write failure")
        await super().write_file(
            community_id=community_id,
            server_id=server_id,
            rel_path=rel_path,
            content=content,
        )


class _EndCommitFails(ServersUnitOfWork):
    """Fails ``times`` of the creation's commits from its second, the ones that
    end it, with an outage (issue #4088).

    The creations below sleep on a fake that returns for one wait more than the
    retry makes, so an extra attempt fails a test instead of parking it.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, times: int
    ) -> None:
        super().__init__(session_factory)
        self.commits = 0
        self._times = times

    async def commit(self) -> None:
        self.commits += 1
        if 2 <= self.commits < 2 + self._times:
            raise OSError("connection reset")
        await super().commit()


class _HydrateRecorder(FakeControlPlane):
    """Records the working set the store serves when the start hydrates."""

    def __init__(self, storage: Storage) -> None:
        super().__init__(place_to=WorkerId(uuid.uuid4()))
        self._storage = storage
        self.hydrated: list[dict[str, bytes]] = []

    async def hydrate(
        self,
        *,
        worker_id: WorkerId,
        community_id: CommunityId,
        server_id: ServerId,
        timeout_seconds: float | None = None,
    ) -> CommandOutcome:
        self.hydrated.append(await _tree(self._storage, community_id, server_id))
        return await super().hydrate(
            worker_id=worker_id,
            community_id=community_id,
            server_id=server_id,
            timeout_seconds=timeout_seconds,
        )


class _SignallingLock(PgLifecycleLock):
    """The real lock, which sets ``contended`` when an acquire finds it held."""

    def __init__(self, engine: AsyncEngine) -> None:
        super().__init__(engine)
        self.contended = asyncio.Event()

    async def _acquire(  # type: ignore[override]
        self, conn: AsyncConnection, key: int, server_id: ServerId
    ) -> None:
        got = await conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        if not got:
            self.contended.set()
            await PgLifecycleLock._acquire(conn, key, server_id)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


async def _seed_community(engine: AsyncEngine) -> CommunityId:
    community = Community(
        id=CommunityCommunityId(uuid.uuid4()),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with CommunityUnitOfWork(create_session_factory(engine)) as uow:
        await uow.communities.add(community)
        await uow.commit()
    return CommunityId(community.id.value)


async def _tree(
    storage: Storage, community_id: CommunityId, server_id: ServerId
) -> dict[str, bytes]:
    """The files a hydrate of the server's working set serves now."""

    source = storage.open_hydrate_source(
        StorageCommunityId(community_id.value), StorageServerId(server_id.value)
    )
    blob = b"".join([chunk async for chunk in source])
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tar:
        for member in tar.getmembers():
            if member.isfile():
                handle = tar.extractfile(member)
                assert handle is not None
                out[member.name] = handle.read()
    return out


def _archive() -> bytes:
    metadata = {
        "format": EXPORT_FORMAT_VERSION,
        "name": "exported",
        "mc_edition": "java",
        "mc_version": "1.21.1",
        "server_type": "vanilla",
        "exported_at": _NOW.isoformat(),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr(EXPORT_METADATA_FILENAME, json.dumps(metadata))
        for name, content in _ARCHIVE_MEMBERS.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _begin_creation(
    race: str,
    engine: AsyncEngine,
    storage: Storage,
    lock: PgLifecycleLock,
    community_id: CommunityId,
) -> tuple[asyncio.Task[Server], _ParkingFileStore, bool]:
    """Start the creation ``race`` names, parked before one of its writes.

    Returns the creation's task, its parking store, and whether the racing start
    has to accept the EULA itself (the seed parks before eula.txt is written).
    """

    park_at = "server.properties" if race == "seed" else "world/region/r.0.0.mca"
    file_store = _ParkingFileStore(storage=storage, park_at=park_at)
    create = CreateServer(
        uow=ServersUnitOfWork(create_session_factory(engine)),
        clock=_FixedClock(),
        version_validator=FakeVersionValidator(),
        file_store=file_store,
        port_range=PortRange(start=25565, end=25664),
        lifecycle_lock=lock,
    )
    if race == "seed":
        task = asyncio.create_task(
            create(
                community_id=community_id,
                name="survival",
                mc_edition="java",
                mc_version="1.21.1",
                server_type="vanilla",
                config={},
                accept_eula=True,
            )
        )
        return task, file_store, True
    importer = ImportServer(create_server=create, file_store=file_store)
    task = asyncio.create_task(
        importer(community_id=community_id, name="survival", content=_archive())
    )
    return task, file_store, False


def _start_server(
    engine: AsyncEngine, storage: Storage, lock: PgLifecycleLock
) -> tuple[StartServer, _HydrateRecorder]:
    plane = _HydrateRecorder(storage)
    start = StartServer(
        uow=ServersUnitOfWork(
            create_session_factory(engine), InProcessRealTimeEvents()
        ),
        control_plane=plane,
        clock=_FixedClock(),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
        file_store=StorageFileStoreAdapter(storage=storage),
        lifecycle_lock=lock,
    )
    return start, plane


async def _load(engine: AsyncEngine, server_id: ServerId) -> Server:
    async with ServersUnitOfWork(create_session_factory(engine)) as uow:
        server = await uow.servers.get_by_id(server_id)
    assert server is not None
    return server


@pytest.mark.parametrize("race", ["seed", "import"])
async def test_a_start_racing_a_creation_hydrates_the_whole_tree(
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
    # No acquire ceiling: this pins that the start waits for the creation, and
    # the suite-wide timeout is the hang guard (as in
    # test_lifecycle_lock_concurrency.py).
    monkeypatch.setattr(lifecycle_lock_mod, "_ACQUIRE_BUDGET_SECONDS", 600.0)
    storage = FsStorage(tmp_path)
    lock = _SignallingLock(engine)
    community_id = await _seed_community(engine)
    creating, creator, accept_eula = _begin_creation(
        race, engine, storage, lock, community_id
    )
    await creator.parked.wait()
    assert creator.server_id is not None
    server_id = creator.server_id
    start, plane = _start_server(engine, storage, lock)

    starting = asyncio.create_task(
        start(community_id=community_id, server_id=server_id, accept_eula=accept_eula)
    )
    # Resume the creation once the start waits for its lock, or once the start
    # is done without waiting: then it hydrated the tree as the parked creation
    # left it.
    contended = asyncio.create_task(lock.contended.wait())
    await asyncio.wait({starting, contended}, return_when=asyncio.FIRST_COMPLETED)
    contended.cancel()
    creator.release.set()
    await creating
    started = await starting

    whole = await _tree(storage, community_id, server_id)
    assert plane.hydrated == [whole]
    assert set(whole) >= (
        {"server.properties", "eula.txt"} | set(_ARCHIVE_MEMBERS)
        if race == "import"
        else {"server.properties", "eula.txt"}
    )
    assert started.desired_state is DesiredState.RUNNING


@pytest.mark.parametrize("race", ["seed", "import"])
async def test_a_start_racing_a_creation_past_its_budget_is_refused(
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
    # A creation that outlasts the start's acquire budget, as a large import
    # can: the start is refused server_busy and nothing is hydrated.
    monkeypatch.setattr(lifecycle_lock_mod, "_ACQUIRE_BUDGET_SECONDS", 0.3)
    monkeypatch.setattr(lifecycle_lock_mod, "_ACQUIRE_POLL_SECONDS", 0.05)
    storage = FsStorage(tmp_path)
    lock = PgLifecycleLock(engine)
    community_id = await _seed_community(engine)
    creating, creator, accept_eula = _begin_creation(
        race, engine, storage, lock, community_id
    )
    await creator.parked.wait()
    assert creator.server_id is not None
    server_id = creator.server_id
    start, plane = _start_server(engine, storage, lock)

    (outcome,) = await asyncio.gather(
        start(community_id=community_id, server_id=server_id, accept_eula=accept_eula),
        return_exceptions=True,
    )
    hydrated_while_creating = list(plane.hydrated)
    creator.release.set()
    await creating

    assert hydrated_while_creating == []
    assert isinstance(outcome, ServerBusyError)
    row = await _load(engine, server_id)
    assert row.desired_state is DesiredState.STOPPED
    assert row.assigned_worker_id is None


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param("seed", id="a-seed-write"),
        pytest.param("import", id="an-import-member-write"),
        pytest.param("end", id="the-commit-that-ends-it"),
    ],
)
async def test_a_start_after_a_failed_creation_is_refused(
    engine: AsyncEngine, tmp_path: Path, failure: str
) -> None:
    # Issue #4065: the creation answers seed_failed and leaves the row, with
    # the working set as far as it got. The start that follows, accepting the
    # EULA as the Web UI's retry does, is refused and hydrates nothing.
    storage = FsStorage(tmp_path)
    lock = PgLifecycleLock(engine)
    community_id = await _seed_community(engine)
    file_store = _FailingFileStore(
        storage=storage,
        fail_at={
            "seed": "server.properties",
            "import": "world/region/r.0.0.mca",
            "end": "",
        }[failure],
    )
    session_factory = create_session_factory(engine)
    create = CreateServer(
        uow=(
            # On every attempt.
            _EndCommitFails(session_factory, times=3)
            if failure == "end"
            else ServersUnitOfWork(session_factory)
        ),
        clock=_FixedClock(),
        version_validator=FakeVersionValidator(),
        file_store=file_store,
        port_range=PortRange(start=25565, end=25664),
        lifecycle_lock=lock,
        sleep=RecordingSleep(returns=3),
    )
    creation = (
        ImportServer(create_server=create, file_store=file_store)(
            community_id=community_id, name="survival", content=_archive()
        )
        if failure == "import"
        else create(
            community_id=community_id,
            name="survival",
            mc_edition="java",
            mc_version="1.21.1",
            server_type="vanilla",
            config={},
            accept_eula=True,
        )
    )
    with pytest.raises(WorkingSetSeedFailedError) as exc_info:
        await creation
    server_id = ServerId(uuid.UUID(exc_info.value.server_id))
    start, plane = _start_server(engine, storage, lock)

    with pytest.raises(ServerCreationIncompleteError):
        await start(community_id=community_id, server_id=server_id, accept_eula=True)

    assert plane.hydrated == []
    row = await _load(engine, server_id)
    assert (row.desired_state, row.assigned_worker_id, row.creation_incomplete) == (
        DesiredState.STOPPED,
        None,
        True,
    )


async def test_a_creation_whose_end_met_an_outage_once_can_start(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    # Issue #4088: the commit that ends the creation is retried in a new unit
    # of work after an outage, so the whole working set is startable.
    storage = FsStorage(tmp_path)
    lock = PgLifecycleLock(engine)
    community_id = await _seed_community(engine)
    uow = _EndCommitFails(create_session_factory(engine), times=1)
    create = CreateServer(
        uow=uow,
        clock=_FixedClock(),
        version_validator=FakeVersionValidator(),
        file_store=StorageFileStoreAdapter(storage=storage),
        port_range=PortRange(start=25565, end=25664),
        lifecycle_lock=lock,
        sleep=RecordingSleep(returns=3),
    )
    server = await create(
        community_id=community_id,
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        config={},
        accept_eula=True,
    )
    assert uow.commits == 3
    assert (await _load(engine, server.id)).creation_incomplete is False
    start, _ = _start_server(engine, storage, lock)

    started = await start(community_id=community_id, server_id=server.id)

    assert started.desired_state is DesiredState.RUNNING
