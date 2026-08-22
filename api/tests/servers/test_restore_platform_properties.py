"""Restore re-applies the platform-managed ``server.properties`` keys (#2621).

A backup archive carries the working set as it was WHEN THE BACKUP WAS TAKEN, so
restoring it republishes that file verbatim — including a ``server-port`` the
server has since been re-ported away from, an ``rcon.port`` the platform no longer
uses, and a resource-pack pointer the assignment row no longer holds. Nothing
downstream re-applies the DB's values, so the file and the DB disagree from the
restore onward and hydrate copies the disagreement forever.

These run :class:`RestoreBackup` over a REAL archive round trip — a real
``FsStorage``, the real backup-archive and file-store adapters — rather than a
synthetic properties file, so the pin covers the publish path the bug actually
travels.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import pytest

from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.file_store import StorageFileStoreAdapter
from mc_server_dashboard_api.servers.application.backups import RestoreBackup
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.resource_pack import (
    ResourcePack,
    ResourcePackAssignment,
    ResourcePackId,
)
from mc_server_dashboard_api.servers.domain.server_properties import RCON_PORT
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.servers.fakes import (
    FakeBackupRepository,
    FakeClock,
    FakePluginCacheStore,
    FakeServerRepository,
    FakeUnitOfWork,
)
from tests.storage.helpers import tar_stream

_NOW = dt.datetime(2026, 8, 21, 12, 0, tzinfo=dt.timezone.utc)
_BASE_URL = "https://mcsd.example"
_ARCHIVE_PROPERTIES = (
    b"server-port=25565\n"
    b"enable-rcon=false\n"
    b"rcon.port=1234\n"
    b"rcon.password=known-secret\n"
    b"resource-pack=https://stale/pack.zip\n"
    b"resource-pack-sha1=stale-sha\n"
    b"require-resource-pack=true\n"
    b"resource-pack-prompt=stale prompt\n"
    b"motd=hi\n"
)


def _server(server_id: ServerId, community_id: CommunityId) -> Server:
    return Server(
        id=server_id,
        community_id=community_id,
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=_NOW,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
        game_port=26590,
    )


def _backup(server_id: ServerId, storage_ref: str) -> Backup:
    return Backup(
        id=BackupId.new(),
        server_id=server_id,
        storage_ref=storage_ref,
        size_bytes=None,
        source=BackupSource.MANUAL,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW,
    )


def _pack(pack_id: ResourcePackId) -> ResourcePack:
    return ResourcePack(
        id=pack_id,
        filename="pack.zip",
        display_name="Pack",
        description=None,
        sha1_hash="live-sha",
        sha256_hash="0" * 64,
        size_bytes=10,
        uploaded_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
    )


async def _publish(
    storage: FsStorage,
    community_id: CommunityId,
    server_id: ServerId,
    files: dict[str, bytes],
) -> None:
    handle = await storage.begin_snapshot(
        StorageCommunityId(community_id.value), StorageServerId(server_id.value)
    )
    await storage.write_snapshot(handle, tar_stream(files))
    await storage.commit_snapshot(handle)


async def _restored_properties(
    tmp_path: Path,
    *,
    archive_files: dict[str, bytes],
    assigned_pack: ResourcePack | None = None,
    require_pack: bool = False,
    pack_prompt: str | None = None,
) -> dict[str, str]:
    """Back up ``archive_files``, restore it, and return the published properties."""

    storage = FsStorage(tmp_path, version_retention=10)
    backup_store = StorageBackupStoreAdapter(storage=storage)
    file_store = StorageFileStoreAdapter(storage=storage)
    community_id, server_id = CommunityId(uuid.uuid4()), ServerId(uuid.uuid4())

    await _publish(storage, community_id, server_id, archive_files)
    storage_ref = uuid.uuid4().hex
    await backup_store.create_from_current(
        community_id=community_id, server_id=server_id, storage_ref=storage_ref
    )
    # A later working set the restore must replace, so the assertions cannot pass
    # on the pre-backup state.
    await _publish(storage, community_id, server_id, {"motd.txt": b"later"})

    servers = FakeServerRepository()
    servers.seed(_server(server_id, community_id))
    backups = FakeBackupRepository()
    backup = _backup(server_id, storage_ref)
    backups.seed(backup)
    uow = FakeUnitOfWork(servers=servers, backups=backups)
    if assigned_pack is not None:
        await uow.resource_packs.add(assigned_pack)
        await uow.resource_packs.add_assignment(
            ResourcePackAssignment(
                server_id=server_id,
                resource_pack_id=assigned_pack.id,
                require_resource_pack=require_pack,
                resource_pack_prompt=pack_prompt,
                assigned_by=uuid.uuid4(),
                created_at=_NOW,
                updated_at=_NOW,
            )
        )

    await RestoreBackup(
        uow=uow,
        backup_store=backup_store,
        file_store=file_store,
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
        public_base_url=_BASE_URL,
        token_generator=lambda: "generated-secret",
    )(community_id=community_id, server_id=server_id, backup_id=backup.id)

    content = await file_store.read_file(
        community_id=community_id, server_id=server_id, rel_path="server.properties"
    )
    return dict(
        line.split("=", 1)
        for line in content.decode().splitlines()
        if "=" in line and not line.startswith("#")
    )


# The publish -> backup -> restore round trip is a real-filesystem path whose every
# step fsyncs, which makes it legitimately slow under disk contention (issue
# #1373). Override the suite-wide cap so disk pressure does not turn a
# slow-but-correct run into a false failure, while still bounding a genuine hang.
pytestmark = pytest.mark.timeout(300)


async def test_restore_republishes_the_db_game_port(tmp_path: Path) -> None:
    props = await _restored_properties(
        tmp_path, archive_files={"server.properties": _ARCHIVE_PROPERTIES}
    )

    assert props["server-port"] == "26590"
    # Everything the platform does NOT own comes back exactly as backed up.
    assert props["motd"] == "hi"


async def test_restore_re_enforces_the_rcon_keys(tmp_path: Path) -> None:
    props = await _restored_properties(
        tmp_path, archive_files={"server.properties": _ARCHIVE_PROPERTIES}
    )

    assert props["enable-rcon"] == "true"
    assert props["rcon.port"] == str(RCON_PORT)
    # The file is rcon.password's only source of truth (#335), so a restored file
    # carrying a working credential keeps it.
    assert props["rcon.password"] == "known-secret"


async def test_restore_clears_pack_keys_the_server_no_longer_holds(
    tmp_path: Path,
) -> None:
    props = await _restored_properties(
        tmp_path, archive_files={"server.properties": _ARCHIVE_PROPERTIES}
    )

    assert "resource-pack" not in props
    assert "resource-pack-sha1" not in props
    assert "require-resource-pack" not in props
    assert "resource-pack-prompt" not in props


async def test_restore_rewrites_the_pack_keys_from_the_assignment(
    tmp_path: Path,
) -> None:
    pack_id = ResourcePackId(uuid.uuid4())
    props = await _restored_properties(
        tmp_path,
        archive_files={"server.properties": _ARCHIVE_PROPERTIES},
        assigned_pack=_pack(pack_id),
    )

    assert props["resource-pack"] == (
        f"{_BASE_URL}/api/public/resource-packs/{pack_id.value}/pack.zip"
    )
    assert props["resource-pack-sha1"] == "live-sha"
    assert props["require-resource-pack"] == "false"
    # The row carries no prompt, so the backup's prompt must not survive.
    assert "resource-pack-prompt" not in props


async def test_restore_seeds_properties_when_the_backup_has_none(
    tmp_path: Path,
) -> None:
    # A backup of a working set with no server.properties restores none, and the
    # worker would then fall back to 25565 -- the very collision this issue is
    # about, reached with a correct DB row. Seed the platform keys instead, which
    # is exactly what a create does.
    props = await _restored_properties(tmp_path, archive_files={"motd.txt": b"only"})

    assert props["server-port"] == "26590"
    assert props["enable-rcon"] == "true"
    assert props["rcon.port"] == str(RCON_PORT)
    assert props["rcon.password"] == "generated-secret"
