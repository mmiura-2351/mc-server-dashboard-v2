"""Use-case tests for deleting dangling backup rows (issue #4107).

A dangling row is a backup row whose archive the store definitively reports
missing: its size HEAD answers NotFound. The #4086 leak left such rows, and
nothing else removes them. :class:`DeleteDanglingBackups` lists them, and with
``apply`` deletes each one under its server's lifecycle lock and audits it. A
HEAD that fails any other way is no verdict: the row is skipped, never deleted.
Exercised against fakes, per TESTING.md Section 4.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import errno
import logging
import uuid
from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.audit.domain.events import Outcome
from mc_server_dashboard_api.audit.domain.operations import (
    BACKUP_DELETE_DANGLING,
    TARGET_BACKUP,
)
from mc_server_dashboard_api.servers.application.backups import (
    DeleteDanglingBackups,
)
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    BackupStorageUnavailableError,
    LifecycleLockLostError,
    ServerBusyError,
)
from mc_server_dashboard_api.servers.domain.lifecycle_lock import (
    HeldLifecycleLock,
    LifecycleLock,
    NullHeldLifecycleLock,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.servers.fakes import (
    FakeBackupArchiveStore,
    FakeLifecycleLock,
    SessionTrackingUnitOfWork,
)

_NOW = dt.datetime(2026, 10, 15, 12, 19, tzinfo=dt.timezone.utc)
_COMMUNITY = CommunityId(uuid.uuid4())


def _server(server_id: ServerId) -> Server:
    return Server(
        id=server_id,
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


def _backup(server_id: ServerId, ref: str, *, minutes: int = 0) -> Backup:
    # The shape the #4086 leak left: a scheduled row with no size.
    return Backup(
        id=BackupId.new(),
        server_id=server_id,
        storage_ref=ref,
        size_bytes=None,
        source=BackupSource.SCHEDULED,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW + dt.timedelta(minutes=minutes),
    )


class _Env:
    """Servers, their rows, and a store holding an archive for each row but those
    named in ``missing``."""

    def __init__(
        self,
        servers: list[Server],
        backups: list[Backup],
        *,
        missing: set[str],
        lifecycle_lock: LifecycleLock | None = None,
    ) -> None:
        self.uow = SessionTrackingUnitOfWork()
        for server in servers:
            self.uow.servers.seed(server)
        for backup in backups:
            self.uow.backups.seed(backup)
        self.store = FakeBackupArchiveStore()
        for backup in backups:
            if backup.storage_ref not in missing:
                self.store.archives.add(backup.storage_ref)
                self.store.bytes_by_ref[backup.storage_ref] = b"x" * 10
        self.audit = RecordingAuditRecorder()
        self.lock = lifecycle_lock or FakeLifecycleLock()
        self.delete = DeleteDanglingBackups(
            uow=self.uow,
            backup_store=self.store,
            audit=self.audit,
            lifecycle_lock=self.lock,
        )

    def rows(self) -> set[BackupId]:
        return set(self.uow.backups.by_id)


class _BusyLifecycleLock(LifecycleLock):
    """Raises ``ServerBusyError`` on the ``busy`` acquisitions (1-based), as the
    real lock does once another operation holds it past the wait budget."""

    def __init__(self, busy: set[int]) -> None:
        self.busy = busy
        self.acquisitions = 0

    @contextlib.asynccontextmanager
    async def hold(self, server_id: ServerId) -> AsyncIterator[HeldLifecycleLock]:
        self.acquisitions += 1
        if self.acquisitions in self.busy:
            raise ServerBusyError(str(server_id.value))
        yield NullHeldLifecycleLock()


async def test_a_dry_run_lists_the_dangling_rows_and_deletes_nothing() -> None:
    sid = ServerId.new()
    dangling = _backup(sid, "gone")
    sound = _backup(sid, "kept", minutes=-5)
    env = _Env([_server(sid)], [dangling, sound], missing={"gone"})

    report = await env.delete(apply=False)

    assert [b.id for b in report.dangling] == [dangling.id]
    assert report.servers_scanned == 1
    assert env.rows() == {dangling.id, sound.id}
    assert env.store.deleted == []
    assert env.audit.events == []


async def test_apply_deletes_each_dangling_row_under_the_lock_and_audits_it() -> None:
    sid = ServerId.new()
    first = _backup(sid, "gone-1")
    second = _backup(sid, "gone-2", minutes=-5)
    sound = _backup(sid, "kept", minutes=-10)
    env = _Env([_server(sid)], [first, second, sound], missing={"gone-1", "gone-2"})
    assert isinstance(env.lock, FakeLifecycleLock)
    # One timeline for the lock, the store and the rows (issue #2515).
    events: list[tuple[ServerId, str]] = []
    env.lock.events = events
    env.store.events = events
    env.uow.backups.events = events

    report = await env.delete(apply=True)

    assert [b.id for b in report.dangling] == [first.id, second.id]
    assert env.rows() == {sound.id}
    # Each deletion falls inside a hold of its own, after a check that the lock
    # is still held; the sound row's hold deletes nothing.
    deletion = [
        (sid, "acquire"),
        (sid, "check"),
        (sid, "delete-archive"),
        (sid, "delete-row"),
        (sid, "release"),
    ]
    assert events == [*deletion, *deletion, (sid, "acquire"), (sid, "release")]
    assert [e.target_id for e in env.audit.events] == [first.id.value, second.id.value]
    for event in env.audit.events:
        assert event.operation == BACKUP_DELETE_DANGLING
        assert event.outcome is Outcome.SUCCESS
        assert event.actor_id is None
        assert event.community_id == _COMMUNITY.value
        assert event.target_type == TARGET_BACKUP


async def test_a_row_the_store_could_not_answer_for_is_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An outage says nothing about the archive, so the row is skipped, never
    # deleted, even when it has no archive; the rest of the pass goes on.
    sid = ServerId.new()
    unanswered = _backup(sid, "unanswered")
    dangling = _backup(sid, "gone", minutes=-5)
    env = _Env([_server(sid)], [unanswered, dangling], missing={"unanswered", "gone"})
    env.store.unavailable_refs.add("unanswered")

    with caplog.at_level(logging.WARNING):
        report = await env.delete(apply=True)

    assert [b.id for b in report.skipped_store_error] == [unanswered.id]
    assert [b.id for b in report.dangling] == [dangling.id]
    assert env.rows() == {unanswered.id}
    assert [e.target_id for e in env.audit.events] == [dangling.id.value]
    assert any(
        r.levelno == logging.WARNING and str(unanswered.id.value) in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize(
    "error",
    [
        BackupStorageUnavailableError("down"),
        TimeoutError("the store did not answer"),
        OSError(errno.EIO, "Input/output error"),
    ],
    ids=["outage", "timeout", "io-error"],
)
async def test_no_answer_but_not_found_ever_deletes(error: Exception) -> None:
    sid = ServerId.new()
    row = _backup(sid, "gone")
    env = _Env([_server(sid)], [row], missing={"gone"})
    env.store.size_error = error

    report = await env.delete(apply=True)

    assert report.dangling == ()
    assert [b.id for b in report.skipped_store_error] == [row.id]
    assert env.rows() == {row.id}
    assert env.store.deleted == []
    assert env.audit.events == []


async def test_a_row_whose_server_is_busy_is_skipped() -> None:
    # A delete in flight holds the lock from its archive removal to its row
    # removal; its row is skipped as busy rather than judged mid-delete.
    sid = ServerId.new()
    in_flight = _backup(sid, "in-flight")
    dangling = _backup(sid, "gone", minutes=-5)
    env = _Env(
        [_server(sid)],
        [in_flight, dangling],
        missing={"in-flight", "gone"},
        lifecycle_lock=_BusyLifecycleLock({1}),
    )

    report = await env.delete(apply=True)

    assert [b.id for b in report.skipped_busy] == [in_flight.id]
    assert [b.id for b in report.dangling] == [dangling.id]
    assert env.rows() == {in_flight.id}
    assert [e.target_id for e in env.audit.events] == [dangling.id.value]


async def test_a_lock_lost_before_the_deletion_deletes_nothing() -> None:
    sid = ServerId.new()
    row = _backup(sid, "gone")
    env = _Env([_server(sid)], [row], missing={"gone"})
    assert isinstance(env.lock, FakeLifecycleLock)
    env.lock.checks_before_loss = 0

    with pytest.raises(LifecycleLockLostError):
        await env.delete(apply=True)

    assert env.rows() == {row.id}
    assert env.store.deleted == []
    assert env.audit.events == []


async def test_a_row_deleted_before_its_lock_is_taken_is_not_reported() -> None:
    # The row is re-read under the lock: one another delete removed after the
    # listing is gone, not dangling, and this pass neither deletes nor audits it.
    sid = ServerId.new()
    row = _backup(sid, "gone")
    env = _Env([_server(sid)], [row], missing={"gone"})

    class _DeletedFirst(LifecycleLock):
        @contextlib.asynccontextmanager
        async def hold(self, server_id: ServerId) -> AsyncIterator[HeldLifecycleLock]:
            env.uow.backups.by_id.pop(row.id, None)
            yield NullHeldLifecycleLock()

    delete = DeleteDanglingBackups(
        uow=env.uow,
        backup_store=env.store,
        audit=env.audit,
        lifecycle_lock=_DeletedFirst(),
    )

    report = await delete(apply=True)

    assert report.dangling == ()
    assert env.audit.events == []


async def test_a_pass_scoped_to_one_server_leaves_the_others() -> None:
    scoped, other = ServerId.new(), ServerId.new()
    in_scope = _backup(scoped, "gone-here")
    elsewhere = _backup(other, "gone-there")
    env = _Env(
        [_server(scoped), _server(other)],
        [in_scope, elsewhere],
        missing={"gone-here", "gone-there"},
    )

    report = await env.delete(apply=True, server_id=scoped)

    assert report.servers_scanned == 1
    assert [b.id for b in report.dangling] == [in_scope.id]
    assert env.rows() == {elsewhere.id}


async def test_an_unscoped_pass_covers_every_server() -> None:
    first, second = ServerId.new(), ServerId.new()
    one = _backup(first, "gone-1")
    two = _backup(second, "gone-2")
    env = _Env(
        [_server(first), _server(second)], [one, two], missing={"gone-1", "gone-2"}
    )

    report = await env.delete(apply=True)

    assert report.servers_scanned == 2
    assert {b.id for b in report.dangling} == {one.id, two.id}
    assert env.rows() == set()
