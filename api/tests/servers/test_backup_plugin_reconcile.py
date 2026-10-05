"""Tests for plugin reconciliation after backup restore (issue #1336).

After ``RestoreBackup`` replaces the filesystem, ``server_plugin`` rows must be
reconciled against the restored working set:

- Orphan rows (DB row but no file on disk) are deleted.
- Ghost files (file on disk but no DB row) are ingested with manifest parsing.
- Shifted records (file exists but checksum changed) are updated.
- A server with no plugins (or an unsupported server type) is a no-op.
- A content directory that could not be LISTED proves nothing about its jars:
  the rows are left alone and the restore reports the reconciliation incomplete
  (issue #3221). Only the typed miss means "no such directory".
- A forced restore of a corrupt backup reconciles like any other (issue #3222).
"""

from __future__ import annotations

import datetime as dt
import errno
import hashlib
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from mc_server_dashboard_api.servers.adapters.file_store import (
    StorageFileStoreAdapter,
)
from mc_server_dashboard_api.servers.application.backups import RestoreBackup
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    BackupCorruptError,
    PluginReconcileIncompleteError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileEntry, FileStore
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from mc_server_dashboard_api.storage.adapters import fs as fs_adapter
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.errors import ObjectStoreUnavailableError
from mc_server_dashboard_api.storage.domain.port import Storage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
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
from tests.storage.fake_s3 import FakeS3Client, FakeS3Store, fake_s3_factory
from tests.storage.helpers import tar_stream

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
    file_store: FileStore | None = None,
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


async def test_restore_preserves_client_only_plugin_records() -> None:
    """A client-only plugin survives reconciliation even with no file on disk.

    Client-only plugins (side == "client") are metadata-only on the server --
    the jar is tracked and cached but never deployed to the working set. Since
    backups capture filesystem state (which excludes client-only jars), their
    DB rows must not be deleted as orphans (#1445).
    """
    server = _server()
    repo, backups, backup, archive = _seed_restore_fixture(server)
    plugins = FakePluginRepository()
    client_mod = _plugin(
        server_id=server.id,
        rel_path="mods/client-mod.jar",
        filename="client-mod.jar",
        display_name="Client Mod",
    )
    client_mod.side = "client"
    plugins.seed(client_mod)
    # No file on disk -- client-only jars are never in the working set.
    file_store = FakeFileStore()
    uow = FakeUnitOfWork(servers=repo, backups=backups, plugins=plugins)

    await _make_restore(uow, archive, file_store=file_store)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )

    rows = await plugins.list_for_server(server.id)
    assert len(rows) == 1
    assert rows[0].id == client_mod.id
    assert rows[0].side == "client"


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


# --- ghost provenance recovery / unknown fallback (#2059) -------------------


async def test_restore_recovers_catalog_provenance_from_checksum() -> None:
    """A ghost jar whose checksum matches a catalog install elsewhere recovers
    its source and source_project_id rather than being asserted local (#2059)."""
    server = _server()
    repo, backups, backup, archive = _seed_restore_fixture(server)
    plugins = FakePluginRepository()
    jar_bytes = _minimal_jar()
    checksum = hashlib.sha512(jar_bytes).hexdigest()
    # The same jar was installed from a catalog on ANOTHER server, so its
    # provenance is recorded under this checksum in the global plugin table.
    catalog_copy = _plugin(
        server_id=ServerId.new(),
        rel_path="mods/catalog.jar",
        filename="catalog.jar",
        checksum_sha512=checksum,
    )
    catalog_copy.source = PluginSource.MODRINTH
    catalog_copy.source_project_id = "AABBCCDD"
    catalog_copy.source_version_id = "VER123"
    catalog_copy.version_number = "1.2.3"
    plugins.seed(catalog_copy)
    # The identical jar lands on the restored server as a ghost (no DB row here).
    file_store = FakeFileStore()
    file_store.files["mods/ghost.jar"] = jar_bytes
    cache = FakePluginCacheStore()
    uow = FakeUnitOfWork(servers=repo, backups=backups, plugins=plugins)

    await _make_restore(uow, archive, file_store=file_store, cache=cache)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )

    rows = await plugins.list_for_server(server.id)
    ghost = next(r for r in rows if r.server_id == server.id)
    assert ghost.source is PluginSource.MODRINTH
    assert ghost.source_project_id == "AABBCCDD"
    assert ghost.source_version_id == "VER123"
    assert ghost.version_number == "1.2.3"


async def test_restore_marks_unrecoverable_ghost_source_unknown() -> None:
    """A ghost jar with no catalog checksum match is marked provenance-unknown,
    never silently asserted as a local upload (#2059)."""
    server = _server()
    repo, backups, backup, archive = _seed_restore_fixture(server)
    plugins = FakePluginRepository()
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
    assert rows[0].source is PluginSource.UNKNOWN
    assert rows[0].source_project_id is None
    assert rows[0].source_version_id is None
    assert rows[0].version_number is None


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


# --- a listing that could not be read (issue #3221) --------------------------


class _UnlistableContentDir(FakeFileStore):
    """A file store whose ``mods`` listing fails the way a store outage does."""

    async def list_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> list[FileEntry]:
        if rel_path == "mods":
            raise OSError(errno.EIO, "temporary directory listing failure")
        return await super().list_dir(
            community_id=community_id, server_id=server_id, rel_path=rel_path
        )


async def test_restore_keeps_plugin_rows_when_the_listing_fails() -> None:
    """A failed listing is not an empty directory: nothing is deleted."""
    server = _server()
    repo, backups, backup, archive = _seed_restore_fixture(server)
    plugins = FakePluginRepository()
    jar = _minimal_jar(b"present")
    tracked = _plugin(
        server_id=server.id,
        rel_path="mods/present.jar",
        filename="present.jar",
        checksum_sha512=hashlib.sha512(jar).hexdigest(),
    )
    plugins.seed(tracked)
    file_store = _UnlistableContentDir()
    file_store.files["mods/present.jar"] = jar
    uow = FakeUnitOfWork(servers=repo, backups=backups, plugins=plugins)

    with pytest.raises(PluginReconcileIncompleteError):
        await _make_restore(uow, archive, file_store=file_store)(
            community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
        )

    assert await plugins.list_for_server(server.id) == [tracked]


# The same two outcomes at the real seam, on both backends: what the application
# sees is whatever StorageFileStoreAdapter hands back from a real Storage adapter,
# so these pin that "no such directory" arrives as the typed miss and a store
# fault arrives as anything else.

_BuildStorage = Callable[[Path, pytest.MonkeyPatch], tuple[Storage, Callable[[], None]]]


def _fs_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Storage, Callable[[], None]]:
    def _break_listing() -> None:
        real = fs_adapter._list_children

        def _failing(target: Path, not_found: str) -> list[Path]:
            if target.name == "mods":
                raise OSError(errno.EIO, "Input/output error")
            return real(target, not_found)

        monkeypatch.setattr(fs_adapter, "_list_children", _failing)

    return FsStorage(tmp_path), _break_listing


def _object_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Storage, Callable[[], None]]:
    def _break_listing() -> None:
        real = FakeS3Client.list_objects

        async def _failing(self: FakeS3Client, prefix: str) -> object:
            if prefix.endswith("/mods/"):
                raise ObjectStoreUnavailableError("object store list failed")
            return await real(self, prefix)

        monkeypatch.setattr(FakeS3Client, "list_objects", _failing)

    return ObjectStorage(fake_s3_factory(FakeS3Store())), _break_listing


_REAL_BACKENDS = pytest.mark.parametrize("build", [_fs_storage, _object_storage])


async def _publish(storage: Storage, server: Server, files: dict[str, bytes]) -> None:
    handle = await storage.begin_snapshot(
        StorageCommunityId(_COMMUNITY.value), StorageServerId(server.id.value)
    )
    await storage.write_snapshot(handle, tar_stream(files))
    await storage.commit_snapshot(handle)


@_REAL_BACKENDS
async def test_a_store_fault_listing_the_content_dir_keeps_plugin_rows(
    build: _BuildStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server()
    repo, backups, backup, archive = _seed_restore_fixture(server)
    storage, break_listing = build(tmp_path, monkeypatch)
    jar = _minimal_jar(b"present")
    await _publish(storage, server, {"mods/present.jar": jar})
    plugins = FakePluginRepository()
    tracked = _plugin(
        server_id=server.id,
        rel_path="mods/present.jar",
        filename="present.jar",
        checksum_sha512=hashlib.sha512(jar).hexdigest(),
    )
    plugins.seed(tracked)
    uow = FakeUnitOfWork(servers=repo, backups=backups, plugins=plugins)
    file_store = StorageFileStoreAdapter(storage=storage)
    break_listing()

    with pytest.raises(PluginReconcileIncompleteError):
        await _make_restore(uow, archive, file_store=file_store)(
            community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
        )

    assert await plugins.list_for_server(server.id) == [tracked]


@_REAL_BACKENDS
async def test_an_absent_content_dir_still_removes_orphan_rows(
    build: _BuildStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server()
    repo, backups, backup, archive = _seed_restore_fixture(server)
    storage, _ = build(tmp_path, monkeypatch)
    # A published working set with no ``mods`` directory in it.
    await _publish(storage, server, {"eula.txt": b"eula=true\n"})
    plugins = FakePluginRepository()
    plugins.seed(
        _plugin(server_id=server.id, rel_path="mods/gone.jar", filename="gone.jar")
    )
    uow = FakeUnitOfWork(servers=repo, backups=backups, plugins=plugins)

    await _make_restore(
        uow, archive, file_store=StorageFileStoreAdapter(storage=storage)
    )(community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id)

    assert await plugins.list_for_server(server.id) == []


# --- forced restore of a corrupt backup (issue #3222) -----------------------


async def test_forced_corrupt_restore_reconciles_plugins() -> None:
    """A corrupt world region does not stop the readable jars being reconciled."""
    server = _server()
    repo, backups, backup, archive = _seed_restore_fixture(server)
    archive.corrupt_refs.add("ref")
    archive.corrupt_count = 2
    plugins = FakePluginRepository()
    old_jar = _minimal_jar(b"old-content")
    new_jar = _minimal_jar(b"new-content")
    orphan = _plugin(server_id=server.id, rel_path="mods/gone.jar", filename="gone.jar")
    shifted = _plugin(
        server_id=server.id,
        rel_path="mods/mod.jar",
        filename="mod.jar",
        checksum_sha512=hashlib.sha512(old_jar).hexdigest(),
    )
    plugins.seed(orphan)
    plugins.seed(shifted)
    file_store = FakeFileStore()
    file_store.files["mods/mod.jar"] = new_jar
    file_store.files["mods/ghost.jar"] = _minimal_jar(b"ghost")
    uow = FakeUnitOfWork(servers=repo, backups=backups, plugins=plugins)

    result = await _make_restore(uow, archive, file_store=file_store)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id, force=True
    )

    rows = {row.rel_path: row for row in await plugins.list_for_server(server.id)}
    assert sorted(rows) == ["mods/ghost.jar", "mods/mod.jar"]
    assert rows["mods/mod.jar"].id == shifted.id
    assert rows["mods/mod.jar"].checksum_sha512 == hashlib.sha512(new_jar).hexdigest()
    # The forced-corruption outcome is reported exactly as before.
    assert (result.forced_corrupt, result.corrupt_count) == (True, 2)
    persisted = await backups.get_by_id(backup.id)
    assert persisted is not None
    assert persisted.health is BackupHealth.QUARANTINED


async def test_refused_corrupt_restore_leaves_plugin_rows_unchanged() -> None:
    """Without ``force`` nothing was published, so nothing is reconciled."""
    server = _server()
    repo, backups, backup, archive = _seed_restore_fixture(server)
    archive.corrupt_refs.add("ref")
    plugins = FakePluginRepository()
    tracked = _plugin(
        server_id=server.id, rel_path="mods/gone.jar", filename="gone.jar"
    )
    plugins.seed(tracked)
    uow = FakeUnitOfWork(servers=repo, backups=backups, plugins=plugins)

    with pytest.raises(BackupCorruptError):
        await _make_restore(uow, archive)(
            community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
        )

    assert await plugins.list_for_server(server.id) == [tracked]


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
