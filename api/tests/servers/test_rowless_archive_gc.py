"""Use-case tests for reclaiming backup archives no row names (issue #4106).

A backup writes its archive before it records its row (#4086), so a crash, or a
failed discard, between the two leaves an archive with no row. No listing shows
it and nothing else removes it while the server lives.
:class:`ReclaimRowlessBackupArchives` lists each server's archives, subtracts the
refs its backup rows name, and deletes each leftover past the grace period, under
the server's lifecycle lock with the rows re-read there. Exercised against fakes,
per TESTING.md Section 4.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import uuid
from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.servers.application.rowless_archive_gc import (
    ReclaimRowlessBackupArchives,
)
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.backup_store import StoredArchive
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    BackupStorageUnavailableError,
    LifecycleLockLostError,
    ServerBusyError,
)
from mc_server_dashboard_api.servers.domain.lifecycle_lock import (
    HeldLifecycleLock,
    LifecycleLock,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.servers.fakes import (
    DatabaseOutageError,
    FakeBackupArchiveStore,
    FakeClock,
    FakeLifecycleLock,
    SessionTrackingUnitOfWork,
)
from tests.store_ownership import FakeStoreOwnership

_NOW = dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.UTC)
_GRACE = dt.timedelta(hours=1)
_COMMUNITY = CommunityId(uuid.uuid4())
_LOGGER = "mc_server_dashboard_api.servers.application.rowless_archive_gc"


def _server() -> Server:
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=_COMMUNITY,
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _row(server_id: ServerId, ref: str) -> Backup:
    return Backup(
        id=BackupId.new(),
        server_id=server_id,
        storage_ref=ref,
        size_bytes=10,
        source=BackupSource.MANUAL,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW,
    )


class _Env:
    """Servers with rows and archives, and the pass over them."""

    def __init__(
        self,
        servers: list[Server],
        *,
        lifecycle_lock: LifecycleLock | None = None,
        ownership: FakeStoreOwnership | None = None,
        store: FakeBackupArchiveStore | None = None,
    ) -> None:
        self.uow = SessionTrackingUnitOfWork()
        for server in servers:
            self.uow.servers.seed(server)
        self.store = store or FakeBackupArchiveStore()
        self.lock = lifecycle_lock or FakeLifecycleLock()
        self.reclaim = ReclaimRowlessBackupArchives(
            uow=self.uow,
            backup_store=self.store,
            clock=FakeClock(_NOW),
            ownership=ownership or FakeStoreOwnership(),
            lifecycle_lock=self.lock,
            grace=_GRACE,
        )

    def archive(self, server: Server, ref: str, *, age: dt.timedelta) -> None:
        self.store.archive_times.setdefault(server.id, {})[ref] = _NOW - age

    def row(self, server: Server, ref: str) -> None:
        self.uow.backups.seed(_row(server.id, ref))

    def deleted(self) -> set[str]:
        return {ref for _server_id, ref in self.store.deleted}


_OLD = _GRACE + dt.timedelta(minutes=1)
_YOUNG = _GRACE - dt.timedelta(minutes=1)


async def test_reclaims_a_rowless_archive_past_the_grace_and_keeps_a_younger_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = _server()
    env = _Env([server])
    env.archive(server, "leftover", age=_OLD)
    env.archive(server, "in-flight", age=_YOUNG)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await env.reclaim()

    assert env.deleted() == {"leftover"}
    # Each reclaimed archive is logged with what went.
    assert any(
        "leftover" in r.getMessage() and str(server.id.value) in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO
    )


async def test_never_deletes_an_archive_a_row_names() -> None:
    server = _server()
    env = _Env([server])
    env.archive(server, "named", age=_OLD)
    env.row(server, "named")
    env.archive(server, "leftover", age=_OLD)

    await env.reclaim()

    assert env.deleted() == {"leftover"}


async def test_a_server_whose_archives_are_all_named_takes_no_lock() -> None:
    server = _server()
    lock = FakeLifecycleLock()
    env = _Env([server], lifecycle_lock=lock)
    env.archive(server, "named", age=_OLD)
    env.row(server, "named")

    await env.reclaim()

    assert lock.events == []


async def test_deletes_under_the_lifecycle_lock_confirmed_before_the_delete() -> None:
    server = _server()
    lock = FakeLifecycleLock()
    env = _Env([server], lifecycle_lock=lock)
    # One timeline for the lock and the store (issue #2515).
    env.store.events = lock.events
    env.archive(server, "leftover", age=_OLD)

    await env.reclaim()

    assert lock.events == [
        (server.id, "acquire"),
        (server.id, "check"),
        (server.id, "delete-archive"),
        (server.id, "release"),
    ]


class _BusyFor(LifecycleLock):
    """Busy for the servers in ``busy``, as the real lock is once another
    operation holds it past the wait budget; a :class:`FakeLifecycleLock` for
    the rest."""

    def __init__(self, busy: set[ServerId]) -> None:
        self.busy = busy
        self.inner = FakeLifecycleLock()

    @contextlib.asynccontextmanager
    async def hold(self, server_id: ServerId) -> AsyncIterator[HeldLifecycleLock]:
        if server_id in self.busy:
            raise ServerBusyError(str(server_id.value))
        async with self.inner.hold(server_id) as held:
            yield held


async def test_a_busy_server_is_skipped_and_the_pass_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    busy, free = _server(), _server()
    env = _Env([busy, free], lifecycle_lock=_BusyFor({busy.id}))
    env.archive(busy, "busy-leftover", age=_OLD)
    env.archive(free, "free-leftover", age=_OLD)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await env.reclaim()

    assert env.deleted() == {"free-leftover"}
    assert any(
        "busy" in record.getMessage() and str(busy.id.value) in record.getMessage()
        for record in caplog.records
    )


class _LostFor(LifecycleLock):
    """Lost for the servers in ``lost``: their held lock fails its first check,
    as the real one does once its session is gone (issue #3927). A
    :class:`FakeLifecycleLock` for the rest."""

    def __init__(self, lost: set[ServerId]) -> None:
        self.lost = lost
        self.inner = FakeLifecycleLock()

    @contextlib.asynccontextmanager
    async def hold(self, server_id: ServerId) -> AsyncIterator[HeldLifecycleLock]:
        async with self.inner.hold(server_id) as held:
            yield _LostHeld() if server_id in self.lost else held


class _LostHeld(HeldLifecycleLock):
    async def ensure_held(self) -> None:
        raise LifecycleLockLostError("lost")


async def test_a_lost_lock_skips_that_server_and_the_pass_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lost, free = _server(), _server()
    env = _Env([lost, free], lifecycle_lock=_LostFor({lost.id}))
    env.archive(lost, "lost-leftover", age=_OLD)
    env.archive(free, "free-leftover", age=_OLD)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await env.reclaim()

    assert env.deleted() == {"free-leftover"}
    assert any(
        record.levelno == logging.WARNING
        and "lost" in record.getMessage()
        and str(lost.id.value) in record.getMessage()
        for record in caplog.records
    )


def _fail_next_call(owner: object, method: str) -> None:
    """Fail the next call of ``owner.method`` as a database outage would, once:
    the calls after it run as before."""

    original = getattr(owner, method)

    async def failing(*_args: object, **_kwargs: object) -> object:
        setattr(owner, method, original)
        raise DatabaseOutageError("database unreachable")

    setattr(owner, method, failing)


def _ended_with_a_warning(caplog: pytest.LogCaptureFixture) -> bool:
    return any(
        record.levelno == logging.WARNING
        and "database" in record.getMessage()
        and "DatabaseOutageError" in record.getMessage()
        for record in caplog.records
    )


async def test_a_database_error_reading_the_servers_ends_the_pass_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = _server()
    env = _Env([server])
    env.archive(server, "leftover", age=_OLD)
    _fail_next_call(env.uow.servers, "list_all")

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await env.reclaim()

    assert env.deleted() == set()
    assert _ended_with_a_warning(caplog)


async def test_a_database_error_reading_a_servers_rows_ends_the_pass_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The fault passes after one read, so a pass that went on with the next
    # server would reclaim its leftover.
    first, second = _server(), _server()
    env = _Env([first, second])
    env.archive(first, "first-leftover", age=_OLD)
    env.archive(second, "second-leftover", age=_OLD)
    _fail_next_call(env.uow.backups, "list_for_server")

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await env.reclaim()

    assert env.deleted() == set()
    assert _ended_with_a_warning(caplog)


async def test_a_database_error_rereading_under_the_lock_ends_the_pass_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first, second = _server(), _server()
    lock = FakeLifecycleLock()
    env = _Env([first, second], lifecycle_lock=lock)
    env.archive(first, "first-leftover", age=_OLD)
    env.archive(second, "second-leftover", age=_OLD)
    # Only the re-read under the lock reads the server row back.
    _fail_next_call(env.uow.servers, "get_by_id")

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await env.reclaim()

    assert env.deleted() == set()
    assert lock.events == [(first.id, "acquire"), (first.id, "release")]
    assert _ended_with_a_warning(caplog)


class _RowLandsAtAcquire(FakeLifecycleLock):
    """Records ``row`` as committed while the pass waits for the lock: a row
    that starts naming an archive between the listing and the lock."""

    def __init__(self, row: Backup) -> None:
        super().__init__()
        self.row = row
        self.uow: SessionTrackingUnitOfWork | None = None

    @contextlib.asynccontextmanager
    async def hold(self, server_id: ServerId) -> AsyncIterator[HeldLifecycleLock]:
        assert self.uow is not None
        self.uow.backups.seed(self.row)
        async with super().hold(server_id) as held:
            yield held


async def test_rereads_the_rows_under_the_lock_before_deleting() -> None:
    server = _server()
    lock = _RowLandsAtAcquire(_row(server.id, "named-meanwhile"))
    env = _Env([server], lifecycle_lock=lock)
    lock.uow = env.uow
    env.archive(server, "named-meanwhile", age=_OLD)
    env.archive(server, "leftover", age=_OLD)

    await env.reclaim()

    assert env.deleted() == {"leftover"}


class _DeletedDuringListing(FakeBackupArchiveStore):
    """Deletes ``server``'s row, its backup rows cascading, while its archives
    are listed: a ``DeleteServer`` that commits after the pass listed the
    servers. ``DeleteServer`` keeps the newest archive with no row (STORAGE.md
    Section 2.1)."""

    def __init__(self, server: Server) -> None:
        super().__init__()
        self.server = server
        self.uow: SessionTrackingUnitOfWork | None = None

    async def list_archives(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> list[StoredArchive]:
        assert self.uow is not None
        if server_id == self.server.id:
            del self.uow.servers.by_id[server_id]
            for row in await self.uow.backups.list_for_server(server_id):
                del self.uow.backups.by_id[row.id]
        return await super().list_archives(
            community_id=community_id, server_id=server_id
        )


async def test_keeps_the_archives_of_a_server_deleted_during_the_pass() -> None:
    deleted, live = _server(), _server()
    store = _DeletedDuringListing(deleted)
    env = _Env([deleted, live], store=store)
    store.uow = env.uow
    env.archive(deleted, "kept-on-delete", age=_OLD)
    env.row(deleted, "kept-on-delete")
    env.archive(live, "leftover", age=_OLD)

    await env.reclaim()

    assert env.deleted() == {"leftover"}


async def test_a_listing_error_aborts_that_servers_pass_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing, healthy = _server(), _server()
    env = _Env([failing, healthy])
    env.archive(failing, "unseen", age=_OLD)
    env.store.list_errors[failing.id] = BackupStorageUnavailableError(
        str(failing.id.value)
    )
    env.archive(healthy, "leftover", age=_OLD)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await env.reclaim()

    assert env.deleted() == {"leftover"}
    assert any(
        record.levelno == logging.WARNING
        and str(failing.id.value) in record.getMessage()
        for record in caplog.records
    )


class _DeleteFails(FakeBackupArchiveStore):
    """Fails the delete of the refs in ``failing``."""

    def __init__(self, failing: set[str]) -> None:
        super().__init__()
        self.failing = failing

    async def delete(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        if storage_ref in self.failing:
            raise BackupStorageUnavailableError(str(server_id.value))
        await super().delete(
            community_id=community_id, server_id=server_id, storage_ref=storage_ref
        )


async def test_a_failed_delete_aborts_that_servers_pass_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing, healthy = _server(), _server()
    env = _Env([failing, healthy], store=_DeleteFails({"stuck"}))
    env.archive(failing, "stuck", age=_OLD)
    env.archive(healthy, "leftover", age=_OLD)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await env.reclaim()

    assert env.deleted() == {"leftover"}
    assert any(
        record.levelno == logging.WARNING and "stuck" in record.getMessage()
        for record in caplog.records
    )


async def test_an_owner_marker_refusal_aborts_the_whole_pass(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = _server()
    env = _Env([server], ownership=FakeStoreOwnership.foreign())
    env.archive(server, "leftover", age=_OLD)

    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        await env.reclaim()

    assert env.deleted() == set()
    assert any(
        record.levelno == logging.ERROR and "refused" in record.getMessage()
        for record in caplog.records
    )
