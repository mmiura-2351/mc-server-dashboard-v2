"""Use-case tests for backup retention (issue #1841).

Pins :class:`SetBackupRetention` (validate -> persist -> immediate prune),
:class:`ClearBackupRetention`, and :class:`PruneScheduledBackups`: the prune
runs under the per-server lifecycle lock, reuses the DeleteBackup semantics
(archive first, metadata row last), audits each deletion as ``backup:delete``
with actor ``None``, and never touches manual / uploaded / event rows. A prune
failure never fails the policy write that triggered it.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.audit.domain.events import Outcome
from mc_server_dashboard_api.audit.domain.operations import (
    BACKUP_DELETE,
    TARGET_BACKUP,
)
from mc_server_dashboard_api.servers.application.backups import (
    ClearBackupRetention,
    PruneScheduledBackups,
    ServerBackupStatistics,
    SetBackupRetention,
)
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidRetentionPolicyError,
    ServerNotFoundError,
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
    FakeClock,
    FakeLifecycleLock,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 7, 10, 12, 0, tzinfo=dt.timezone.utc)
_COMMUNITY = CommunityId(uuid.uuid4())


def _server() -> Server:
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=_COMMUNITY,
        name=ServerName("srv"),
        mc_edition="java",
        mc_version="1.21",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=_NOW,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _backup(
    server_id: ServerId,
    created_at: dt.datetime,
    *,
    source: BackupSource = BackupSource.SCHEDULED,
) -> Backup:
    return Backup(
        id=BackupId.new(),
        server_id=server_id,
        storage_ref=f"ref-{uuid.uuid4().hex}",
        size_bytes=10,
        source=source,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=created_at,
    )


class _Env:
    def __init__(self, *, store: FakeBackupArchiveStore | None = None) -> None:
        self.uow = FakeUnitOfWork()
        self.store = store or FakeBackupArchiveStore()
        self.audit = RecordingAuditRecorder()
        self.lock = FakeLifecycleLock()
        self.clock = FakeClock(_NOW)
        self.prune = PruneScheduledBackups(
            uow=self.uow,
            backup_store=self.store,
            audit=self.audit,
            clock=self.clock,
            lifecycle_lock=self.lock,
        )
        self.set_retention = SetBackupRetention(uow=self.uow, prune=self.prune)
        self.clear_retention = ClearBackupRetention(uow=self.uow)
        self.server = _server()
        self.uow.servers.seed(self.server)

    def seed_backup(self, backup: Backup) -> None:
        self.uow.backups.seed(backup)
        self.store.archives.add(backup.storage_ref)
        self.store.bytes_by_ref[backup.storage_ref] = b"x" * 10


class _FailingDeleteStore(FakeBackupArchiveStore):
    async def delete(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        raise RuntimeError("storage down")


# --- SetBackupRetention ------------------------------------------------------


async def test_set_retention_persists_canonical_keep_last_shape() -> None:
    env = _Env()

    policy = await env.set_retention(
        community_id=_COMMUNITY, server_id=env.server.id, keep_last=3
    )

    assert policy.to_json() == {"keep_last": 3}
    assert env.uow.servers.by_id[env.server.id].backup_retention == {"keep_last": 3}


async def test_set_retention_persists_canonical_tiered_shape() -> None:
    env = _Env()

    await env.set_retention(
        community_id=_COMMUNITY, server_id=env.server.id, daily=7, weekly=4
    )

    assert env.uow.servers.by_id[env.server.id].backup_retention == {
        "daily": 7,
        "weekly": 4,
        "monthly": 0,
    }


async def test_set_retention_rejects_invalid_policy_and_stores_nothing() -> None:
    env = _Env()

    with pytest.raises(InvalidRetentionPolicyError):
        await env.set_retention(
            community_id=_COMMUNITY, server_id=env.server.id, keep_last=0
        )

    assert env.uow.servers.by_id[env.server.id].backup_retention is None


async def test_set_retention_unknown_server_is_not_found() -> None:
    env = _Env()

    with pytest.raises(ServerNotFoundError):
        await env.set_retention(
            community_id=_COMMUNITY, server_id=ServerId(uuid.uuid4()), keep_last=3
        )


async def test_set_retention_cross_community_is_not_found() -> None:
    env = _Env()

    with pytest.raises(ServerNotFoundError):
        await env.set_retention(
            community_id=CommunityId(uuid.uuid4()),
            server_id=env.server.id,
            keep_last=3,
        )


async def test_set_retention_prunes_immediately() -> None:
    # With keep-3, a 4th scheduled backup triggers pruning of the oldest
    # scheduled one while the manual row survives (issue #1841 acceptance).
    env = _Env()
    oldest = _backup(env.server.id, _NOW - dt.timedelta(days=4))
    manual = _backup(
        env.server.id, _NOW - dt.timedelta(days=9), source=BackupSource.MANUAL
    )
    survivors = [_backup(env.server.id, _NOW - dt.timedelta(days=i)) for i in range(3)]
    for backup in (oldest, manual, *survivors):
        env.seed_backup(backup)

    await env.set_retention(
        community_id=_COMMUNITY, server_id=env.server.id, keep_last=3
    )

    remaining = {b.id.value for b in env.uow.backups.by_id.values()}
    assert oldest.id.value not in remaining
    assert manual.id.value in remaining
    assert all(s.id.value in remaining for s in survivors)
    # The archive went first (idempotent), then the metadata row.
    assert (env.server.id, oldest.storage_ref) in env.store.deleted


async def test_set_retention_survives_a_prune_failure() -> None:
    # The policy write is the operation; the immediate prune is best-effort
    # (the next successful scheduled backup re-prunes).
    env = _Env(store=_FailingDeleteStore())
    env.seed_backup(_backup(env.server.id, _NOW - dt.timedelta(days=2)))
    env.seed_backup(_backup(env.server.id, _NOW - dt.timedelta(days=1)))

    await env.set_retention(
        community_id=_COMMUNITY, server_id=env.server.id, keep_last=1
    )

    assert env.uow.servers.by_id[env.server.id].backup_retention == {"keep_last": 1}
    # Nothing was deleted (the archive delete failed before the row delete).
    assert len(env.uow.backups.by_id) == 2


# --- ClearBackupRetention ----------------------------------------------------


async def test_clear_retention_nulls_the_policy() -> None:
    env = _Env()
    env.server.backup_retention = {"keep_last": 3}

    await env.clear_retention(community_id=_COMMUNITY, server_id=env.server.id)

    assert env.uow.servers.by_id[env.server.id].backup_retention is None


async def test_clear_retention_unknown_server_is_not_found() -> None:
    env = _Env()

    with pytest.raises(ServerNotFoundError):
        await env.clear_retention(
            community_id=_COMMUNITY, server_id=ServerId(uuid.uuid4())
        )


# --- PruneScheduledBackups ---------------------------------------------------


async def test_prune_without_policy_is_a_noop() -> None:
    env = _Env()
    env.seed_backup(_backup(env.server.id, _NOW - dt.timedelta(days=1)))

    pruned = await env.prune(community_id=_COMMUNITY, server_id=env.server.id)

    assert pruned == []
    assert env.store.deleted == []
    assert env.audit.events == []


async def test_prune_with_malformed_persisted_policy_is_a_noop() -> None:
    # A persisted policy that fails validation should be impossible (writes
    # validate), but if one slips in the prune skips rather than guesses.
    env = _Env()
    env.server.backup_retention = {"bogus": 1}
    env.seed_backup(_backup(env.server.id, _NOW - dt.timedelta(days=1)))

    pruned = await env.prune(community_id=_COMMUNITY, server_id=env.server.id)

    assert pruned == []
    assert env.store.deleted == []


async def test_prune_unknown_server_is_not_found() -> None:
    env = _Env()

    with pytest.raises(ServerNotFoundError):
        await env.prune(community_id=_COMMUNITY, server_id=ServerId(uuid.uuid4()))


async def test_prune_keep_n_deletes_archive_then_row_and_audits() -> None:
    env = _Env()
    env.server.backup_retention = {"keep_last": 2}
    oldest = _backup(env.server.id, _NOW - dt.timedelta(days=3))
    kept = [
        _backup(env.server.id, _NOW - dt.timedelta(days=1)),
        _backup(env.server.id, _NOW),
    ]
    for backup in (oldest, *kept):
        env.seed_backup(backup)

    pruned = await env.prune(community_id=_COMMUNITY, server_id=env.server.id)

    assert [b.id.value for b in pruned] == [oldest.id.value]
    # Archive removed and row gone.
    assert (env.server.id, oldest.storage_ref) in env.store.deleted
    assert oldest.storage_ref not in env.store.archives
    assert oldest.id not in env.uow.backups.by_id
    # Each deletion is audited: backup:delete, SUCCESS, actor None (retention
    # prune has no operator behind it), scoped to the community, targeting the
    # pruned backup.
    assert len(env.audit.events) == 1
    event = env.audit.events[0]
    assert event.operation == BACKUP_DELETE
    assert event.outcome is Outcome.SUCCESS
    assert event.actor_id is None
    assert event.community_id == _COMMUNITY.value
    assert event.target_type == TARGET_BACKUP
    assert event.target_id == oldest.id.value


async def test_prune_tiered_policy_applies_bucket_selection() -> None:
    env = _Env()
    env.server.backup_retention = {"daily": 1, "weekly": 0, "monthly": 0}
    today_new = _backup(env.server.id, _NOW)
    today_old = _backup(env.server.id, _NOW - dt.timedelta(hours=3))
    for backup in (today_new, today_old):
        env.seed_backup(backup)

    pruned = await env.prune(community_id=_COMMUNITY, server_id=env.server.id)

    assert [b.id.value for b in pruned] == [today_old.id.value]
    assert today_new.id in env.uow.backups.by_id


async def test_prune_statistics_reflect_the_pruned_state() -> None:
    env = _Env()
    env.server.backup_retention = {"keep_last": 1}
    for i in range(3):
        env.seed_backup(_backup(env.server.id, _NOW - dt.timedelta(days=i)))

    await env.prune(community_id=_COMMUNITY, server_id=env.server.id)

    stats = await ServerBackupStatistics(uow=env.uow, backup_store=env.store)(
        community_id=_COMMUNITY, server_id=env.server.id
    )
    assert stats.count == 1
    assert stats.total_bytes == 10


async def test_prune_holds_the_lifecycle_lock_around_its_work() -> None:
    env = _Env()
    env.server.backup_retention = {"keep_last": 1}
    env.seed_backup(_backup(env.server.id, _NOW - dt.timedelta(days=1)))
    env.seed_backup(_backup(env.server.id, _NOW))

    await env.prune(community_id=_COMMUNITY, server_id=env.server.id)

    assert env.lock.events == [(env.server.id, "acquire"), (env.server.id, "release")]
