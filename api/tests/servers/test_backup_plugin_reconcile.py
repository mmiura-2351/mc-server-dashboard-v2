"""Tests for plugin reconciliation after backup restore (issue #1336).

After ``RestoreBackup`` replaces the filesystem, ``server_plugin`` rows must be
reconciled against the restored working set:

- Orphan rows (DB row but no file on disk) are deleted.
- Ghost files (file on disk but no DB row) are ingested with manifest parsing.
- Shifted records (file exists but checksum changed) are updated.
- A server with no plugins (or an unsupported server type) is a no-op.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid

from mc_server_dashboard_api.servers.application.backups import RestoreBackup
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.servers.fakes import (
    FakeBackupArchiveStore,
    FakeBackupRepository,
    FakeClock,
    FakeFileStore,
    FakePluginCacheStore,
    FakePluginRepository,
    FakeServerRepository,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 20, 12, 0, tzinfo=dt.timezone.utc)
_COMMUNITY = CommunityId(uuid.uuid4())


def _server(
    *,
    server_type: ServerType = ServerType.FABRIC,
    server_id: ServerId | None = None,
) -> Server:
    return Server(
        id=server_id or ServerId.new(),
        community_id=_COMMUNITY,
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=server_type,
        execution_backend=ExecutionBackend.HOST_PROCESS,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _backup(server_id: ServerId) -> Backup:
    return Backup(
        id=BackupId.new(),
        server_id=server_id,
        storage_ref="ref",
        size_bytes=None,
        source=BackupSource.MANUAL,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW,
    )


def _plugin(
    *,
    server_id: ServerId,
    rel_path: str = "mods/test.jar",
    filename: str = "test.jar",
    display_name: str = "Test",
    checksum_sha512: str = "abc",
    sha256: str | None = None,
) -> ServerPlugin:
    return ServerPlugin(
        id=PluginId.new(),
        server_id=server_id,
        rel_path=rel_path,
        filename=filename,
        display_name=display_name,
        description=None,
        loader_type=LoaderType.MOD,
        source=PluginSource.LOCAL,
        source_project_id=None,
        source_version_id=None,
        version_number=None,
        checksum_sha512=checksum_sha512,
        sha256=sha256,
        size_bytes=100,
        enabled=True,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _make_restore(
    uow: FakeUnitOfWork,
    archive: FakeBackupArchiveStore,
    file_store: FakeFileStore | None = None,
    cache: FakePluginCacheStore | None = None,
    clock: FakeClock | None = None,
) -> RestoreBackup:
    return RestoreBackup(
        uow=uow,
        backup_store=archive,
        file_store=file_store or FakeFileStore(),
        cache=cache or FakePluginCacheStore(),
        clock=clock or FakeClock(_NOW),
    )


def _seed_restore_fixture(
    server: Server,
) -> tuple[FakeServerRepository, FakeBackupRepository, Backup, FakeBackupArchiveStore]:
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    backup = _backup(server.id)
    backups.seed(backup)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    return repo, backups, backup, archive


# --- orphan removal ---------------------------------------------------------


async def test_restore_removes_orphan_plugin_records() -> None:
    """DB row whose file no longer exists after restore is deleted."""
    server = _server()
    repo, backups, backup, archive = _seed_restore_fixture(server)
    plugins = FakePluginRepository()
    orphan = _plugin(server_id=server.id, rel_path="mods/gone.jar", filename="gone.jar")
    plugins.seed(orphan)
    # The file "mods/gone.jar" is NOT in the file store after restore.
    file_store = FakeFileStore()
    uow = FakeUnitOfWork(servers=repo, backups=backups, plugins=plugins)

    await _make_restore(uow, archive, file_store=file_store)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )

    assert await plugins.get_by_id(server.id, orphan.id) is None


async def test_restore_preserves_disabled_plugin_records() -> None:
    """A disabled plugin (.jar.disabled on disk, matching DB row) survives."""
    server = _server()
    repo, backups, backup, archive = _seed_restore_fixture(server)
    plugins = FakePluginRepository()
    jar_bytes = _minimal_jar()
    disabled = _plugin(
        server_id=server.id,
        rel_path="mods/mod.jar.disabled",
        filename="mod.jar",
        display_name="mod",
        checksum_sha512=hashlib.sha512(jar_bytes).hexdigest(),
    )
    disabled.enabled = False
    plugins.seed(disabled)
    # The disabled file IS on disk with the .disabled suffix.
    file_store = FakeFileStore()
    file_store.files["mods/mod.jar.disabled"] = jar_bytes
    uow = FakeUnitOfWork(servers=repo, backups=backups, plugins=plugins)

    await _make_restore(uow, archive, file_store=file_store)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )

    rows = await plugins.list_for_server(server.id)
    assert len(rows) == 1
    assert rows[0].id == disabled.id


# --- ghost ingestion --------------------------------------------------------


async def test_restore_ingests_ghost_plugin_files() -> None:
    """A .jar on disk with no DB row is ingested as a new plugin record."""
    server = _server()
    repo, backups, backup, archive = _seed_restore_fixture(server)
    plugins = FakePluginRepository()
    # Place a ghost jar in the content directory (no matching DB row).
    jar_bytes = _minimal_jar()
    file_store = FakeFileStore()
    file_store.files["mods/ghost.jar"] = jar_bytes
    cache = FakePluginCacheStore()
    uow = FakeUnitOfWork(servers=repo, backups=backups, plugins=plugins)

    await _make_restore(uow, archive, file_store=file_store, cache=cache)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )

    rows = await plugins.list_for_server(server.id)
    assert len(rows) == 1
    assert rows[0].rel_path == "mods/ghost.jar"
    assert rows[0].filename == "ghost.jar"
    assert rows[0].enabled is True
    assert rows[0].checksum_sha512 == hashlib.sha512(jar_bytes).hexdigest()


async def test_restore_ingests_ghost_disabled_plugin_file() -> None:
    """A .jar.disabled on disk with no DB row is ingested as disabled."""
    server = _server()
    repo, backups, backup, archive = _seed_restore_fixture(server)
    plugins = FakePluginRepository()
    jar_bytes = _minimal_jar()
    file_store = FakeFileStore()
    file_store.files["mods/mod.jar.disabled"] = jar_bytes
    cache = FakePluginCacheStore()
    uow = FakeUnitOfWork(servers=repo, backups=backups, plugins=plugins)

    await _make_restore(uow, archive, file_store=file_store, cache=cache)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )

    rows = await plugins.list_for_server(server.id)
    assert len(rows) == 1
    assert rows[0].rel_path == "mods/mod.jar.disabled"
    assert rows[0].filename == "mod.jar"
    assert rows[0].display_name == "mod"
    assert rows[0].enabled is False


# --- no plugins (no-op) -----------------------------------------------------


async def test_restore_with_no_plugins_is_noop() -> None:
    """A server with no plugin rows and no jar files: nothing happens."""
    server = _server()
    repo, backups, backup, archive = _seed_restore_fixture(server)
    file_store = FakeFileStore()
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    result = await _make_restore(uow, archive, file_store=file_store)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )

    assert result.forced_corrupt is False
    rows = await uow.plugins.list_for_server(server.id)
    assert rows == []


# --- idempotent (no changes) ------------------------------------------------


async def test_restore_with_unchanged_plugins_is_idempotent() -> None:
    """When the restored filesystem matches the DB, nothing is mutated."""
    server = _server()
    repo, backups, backup, archive = _seed_restore_fixture(server)
    plugins = FakePluginRepository()
    jar_bytes = _minimal_jar()
    checksum = hashlib.sha512(jar_bytes).hexdigest()
    existing = _plugin(
        server_id=server.id,
        rel_path="mods/existing.jar",
        filename="existing.jar",
        display_name="existing",
        checksum_sha512=checksum,
    )
    plugins.seed(existing)
    file_store = FakeFileStore()
    file_store.files["mods/existing.jar"] = jar_bytes
    uow = FakeUnitOfWork(servers=repo, backups=backups, plugins=plugins)

    await _make_restore(uow, archive, file_store=file_store)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )

    rows = await plugins.list_for_server(server.id)
    assert len(rows) == 1
    assert rows[0].id == existing.id


# --- unsupported server type (no-op) ----------------------------------------


async def test_restore_vanilla_server_skips_plugin_reconciliation() -> None:
    """Vanilla servers don't support plugins; reconciliation is a no-op."""
    server = _server(server_type=ServerType.VANILLA)
    repo, backups, backup, archive = _seed_restore_fixture(server)
    file_store = FakeFileStore()
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    result = await _make_restore(uow, archive, file_store=file_store)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )

    assert result.forced_corrupt is False


# --- shifted records --------------------------------------------------------


async def test_restore_updates_shifted_plugin_checksum() -> None:
    """When the file exists but its content changed, update the DB row."""
    server = _server()
    repo, backups, backup, archive = _seed_restore_fixture(server)
    plugins = FakePluginRepository()
    old_jar = _minimal_jar(b"old-content")
    new_jar = _minimal_jar(b"new-content")
    existing = _plugin(
        server_id=server.id,
        rel_path="mods/mod.jar",
        filename="mod.jar",
        display_name="mod",
        checksum_sha512=hashlib.sha512(old_jar).hexdigest(),
    )
    plugins.seed(existing)
    # After restore, the file has different content.
    file_store = FakeFileStore()
    file_store.files["mods/mod.jar"] = new_jar
    cache = FakePluginCacheStore()
    uow = FakeUnitOfWork(servers=repo, backups=backups, plugins=plugins)

    await _make_restore(uow, archive, file_store=file_store, cache=cache)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )

    rows = await plugins.list_for_server(server.id)
    assert len(rows) == 1
    assert rows[0].id == existing.id
    assert rows[0].checksum_sha512 == hashlib.sha512(new_jar).hexdigest()


# --- helpers ----------------------------------------------------------------


def _minimal_jar(extra: bytes = b"") -> bytes:
    """Create a minimal jar (zip) file for testing."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        if extra:
            zf.writestr("extra", extra)
    return buf.getvalue()
