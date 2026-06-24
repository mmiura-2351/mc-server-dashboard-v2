"""Integration tests for the servers FileStore seam over real fs Storage.

Binds :class:`StorageFileStoreAdapter` to a real :class:`FsStorage` (no fakes on
the Storage side) and verifies the at-rest path the file use cases drive: a
versioned edit round-trip (write -> history -> rollback) and the error
translation (missing path -> ServerFileNotFoundError, traversal ->
InvalidFilePathError, FR-FILE-4).
"""

from __future__ import annotations

import io
import uuid
import zipfile
from pathlib import Path

import pytest

from mc_server_dashboard_api.servers.adapters.file_store import StorageFileStoreAdapter
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidFilePathError,
    ServerFileNotFoundError,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    RelPath,
    VersionId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.storage.helpers import healthy_region_bytes, publish


def _scope() -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4()


async def _seed(storage: FsStorage, community: uuid.UUID, server: uuid.UUID) -> None:
    await publish(
        storage,
        StorageCommunityId(community),
        StorageServerId(server),
        {"server.properties": b"motd=original"},
    )


async def test_read_returns_published_bytes(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)

    out = await adapter.read_file(
        community_id=CommunityId(community),
        server_id=ServerId(server),
        rel_path="server.properties",
    )
    assert out == b"motd=original"


async def test_open_file_stream_round_trips_bytes(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)

    stream = adapter.open_file_stream(
        community_id=CommunityId(community),
        server_id=ServerId(server),
        rel_path="server.properties",
    )
    out = b"".join([chunk async for chunk in stream])
    assert out == b"motd=original"


async def test_open_file_stream_missing_translates_to_file_not_found(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)

    stream = adapter.open_file_stream(
        community_id=CommunityId(community),
        server_id=ServerId(server),
        rel_path="nope.txt",
    )
    with pytest.raises(ServerFileNotFoundError):
        await anext(stream)


async def test_edit_history_rollback_round_trip(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)
    cid, sid = CommunityId(community), ServerId(server)

    # Two edits, each retaining the prior version.
    await adapter.write_file(
        community_id=cid,
        server_id=sid,
        rel_path="server.properties",
        content=b"motd=v1",
    )
    await adapter.write_file(
        community_id=cid,
        server_id=sid,
        rel_path="server.properties",
        content=b"motd=v2",
    )

    current = await adapter.read_file(
        community_id=cid, server_id=sid, rel_path="server.properties"
    )
    assert current == b"motd=v2"

    versions = await adapter.list_versions(
        community_id=cid, server_id=sid, rel_path="server.properties"
    )
    # original + v1 retained before each overwrite (newest-first).
    assert len(versions) == 2

    # Roll back to the oldest retained version (the original content).
    await adapter.rollback(
        community_id=cid,
        server_id=sid,
        rel_path="server.properties",
        version_id=versions[-1],
    )
    rolled = await adapter.read_file(
        community_id=cid, server_id=sid, rel_path="server.properties"
    )
    assert rolled == b"motd=original"


async def test_edit_round_trips_against_populated_snapshot(tmp_path: Path) -> None:
    """An at-rest edit on a populated snapshot persists and reads back (issue #542).

    Live verification found that an at-rest write against a real populated
    snapshot store crashed; this exercises the same path end-to-end (write the
    edited bytes, read them back unchanged) so a regression to the IsADirectoryError
    crash is caught.
    """

    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)
    cid, sid = CommunityId(community), ServerId(server)

    await adapter.write_file(
        community_id=cid,
        server_id=sid,
        rel_path="server.properties",
        content=b"motd=edited",
    )
    out = await adapter.read_file(
        community_id=cid, server_id=sid, rel_path="server.properties"
    )
    assert out == b"motd=edited"

    # A brand-new file under the populated snapshot also lands and reads back.
    await adapter.write_file(
        community_id=cid, server_id=sid, rel_path="marker.txt", content=b"hi"
    )
    assert (
        await adapter.read_file(community_id=cid, server_id=sid, rel_path="marker.txt")
        == b"hi"
    )


async def test_write_to_root_is_invalid_path(tmp_path: Path) -> None:
    """Writing the working-set root (``path="."``) is a clean InvalidFilePathError.

    The file route's ``path`` defaults to ``"."`` (the working-set root, a
    directory). An at-rest write there used to reach the atomic rename and raise an
    unhandled IsADirectoryError (the bare-500, issue #542); the seam now surfaces a
    mapped InvalidFilePathError (422 invalid_path) and leaves the snapshot intact.
    """

    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)
    cid, sid = CommunityId(community), ServerId(server)

    for root in (".", ""):
        with pytest.raises(InvalidFilePathError):
            await adapter.write_file(
                community_id=cid, server_id=sid, rel_path=root, content=b"x"
            )
    # The published copy is untouched by the refused write.
    assert (
        await adapter.read_file(
            community_id=cid, server_id=sid, rel_path="server.properties"
        )
        == b"motd=original"
    )


async def test_write_on_never_snapshotted_server_succeeds(tmp_path: Path) -> None:
    """An at-rest edit before any snapshot initializes the first version (issue #205).

    A server that crashed before its first snapshot has no published working set;
    the EULA-repair edit must succeed end-to-end (no unmapped NotFoundError → 500),
    leaving the file readable.
    """

    storage = FsStorage(tmp_path)
    community, server = _scope()
    adapter = StorageFileStoreAdapter(storage=storage)
    cid, sid = CommunityId(community), ServerId(server)

    await adapter.write_file(
        community_id=cid, server_id=sid, rel_path="eula.txt", content=b"eula=true"
    )
    out = await adapter.read_file(community_id=cid, server_id=sid, rel_path="eula.txt")
    assert out == b"eula=true"


async def test_list_dir_on_never_snapshotted_server_is_empty(tmp_path: Path) -> None:
    """An at-rest listing before any snapshot is empty, not an error (issue #205)."""

    storage = FsStorage(tmp_path)
    community, server = _scope()
    adapter = StorageFileStoreAdapter(storage=storage)

    entries = await adapter.list_dir(
        community_id=CommunityId(community), server_id=ServerId(server), rel_path="."
    )
    assert entries == []


async def test_list_dir_browses_root(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await publish(
        storage,
        StorageCommunityId(community),
        StorageServerId(server),
        {"server.properties": b"x", "world/level.dat": b"y"},
    )
    adapter = StorageFileStoreAdapter(storage=storage)

    entries = await adapter.list_dir(
        community_id=CommunityId(community),
        server_id=ServerId(server),
        rel_path=".",
    )
    names = {e.name for e in entries}
    assert names == {"server.properties", "world"}


async def test_download_dir_streams_zip_of_subtree(tmp_path: Path) -> None:
    """The directory-download zip contains the subtree, with relative arcnames."""

    storage = FsStorage(tmp_path)
    community, server = _scope()
    await publish(
        storage,
        StorageCommunityId(community),
        StorageServerId(server),
        {
            "server.properties": b"top",
            "world/level.dat": b"world-bytes",
            # A structurally valid region: the publish path now runs the integrity
            # gate (#739), so a garbage-byte ``.mca`` would be refused before this
            # download could run.
            "world/region/r.0.0.mca": healthy_region_bytes(),
        },
    )
    adapter = StorageFileStoreAdapter(storage=storage)

    stream = adapter.download_dir(
        community_id=CommunityId(community),
        server_id=ServerId(server),
        rel_path="world",
    )
    blob = b"".join([chunk async for chunk in stream])

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        contents = {name: zf.read(name) for name in zf.namelist()}
    assert contents == {
        "level.dat": b"world-bytes",
        "region/r.0.0.mca": healthy_region_bytes(),
    }


async def test_download_dir_root_zips_whole_tree(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await publish(
        storage,
        StorageCommunityId(community),
        StorageServerId(server),
        {"a.txt": b"a", "sub/b.txt": b"b"},
    )
    adapter = StorageFileStoreAdapter(storage=storage)

    stream = adapter.download_dir(
        community_id=CommunityId(community),
        server_id=ServerId(server),
        rel_path=".",
    )
    blob = b"".join([chunk async for chunk in stream])
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
    assert names == {"a.txt", "sub/b.txt"}


async def test_export_dir_appends_extra_entries(tmp_path: Path) -> None:
    """The export zip carries the working set plus the in-memory extra entries."""

    storage = FsStorage(tmp_path)
    community, server = _scope()
    await publish(
        storage,
        StorageCommunityId(community),
        StorageServerId(server),
        {"server.properties": b"top", "world/level.dat": b"world-bytes"},
    )
    adapter = StorageFileStoreAdapter(storage=storage)

    stream = adapter.export_dir(
        community_id=CommunityId(community),
        server_id=ServerId(server),
        rel_path=".",
        extra=[("export_metadata.json", b'{"format": 1}')],
    )
    blob = b"".join([chunk async for chunk in stream])
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        contents = {name: zf.read(name) for name in zf.namelist()}
    assert contents == {
        "server.properties": b"top",
        "world/level.dat": b"world-bytes",
        "export_metadata.json": b'{"format": 1}',
    }


async def test_download_dir_missing_is_file_not_found(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)

    stream = adapter.download_dir(
        community_id=CommunityId(community),
        server_id=ServerId(server),
        rel_path="nope",
    )
    with pytest.raises(ServerFileNotFoundError):
        async for _ in stream:
            pass


async def test_delete_file_removes_and_retains_version(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await publish(
        storage,
        StorageCommunityId(community),
        StorageServerId(server),
        {"a.txt": b"gone", "b.txt": b"stays"},
    )
    adapter = StorageFileStoreAdapter(storage=storage)
    cid, sid = CommunityId(community), ServerId(server)

    await adapter.delete_file(community_id=cid, server_id=sid, rel_path="a.txt")

    with pytest.raises(ServerFileNotFoundError):
        await adapter.read_file(community_id=cid, server_id=sid, rel_path="a.txt")
    assert (
        await adapter.read_file(community_id=cid, server_id=sid, rel_path="b.txt")
        == b"stays"
    )
    # The deleted content is retained as a version (reversible delete).
    versions = await adapter.list_versions(
        community_id=cid, server_id=sid, rel_path="a.txt"
    )
    assert len(versions) == 1


async def test_retain_if_changed_dedups_repeated_identical_snapshots(
    tmp_path: Path,
) -> None:
    # The running-edit authoritative snapshot (#351): repeated retain_if_changed
    # calls on an UNCHANGED current/ file retain exactly one version, not one per
    # call, so the bounded ring is not churned with identical copies.
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)
    cid, sid = CommunityId(community), ServerId(server)

    for _ in range(5):
        await adapter.retain_if_changed(
            community_id=cid, server_id=sid, rel_path="server.properties"
        )

    versions = await adapter.list_versions(
        community_id=cid, server_id=sid, rel_path="server.properties"
    )
    assert len(versions) == 1
    # The retained version is the authoritative content, and current/ is untouched.
    assert (
        await adapter.read_file(
            community_id=cid, server_id=sid, rel_path="server.properties"
        )
        == b"motd=original"
    )


async def test_retain_if_changed_retains_again_when_authoritative_changes(
    tmp_path: Path,
) -> None:
    # A genuinely changed authoritative copy retains a NEW version (the dedup is
    # only against the newest retained version, #351).
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)
    cid, sid = CommunityId(community), ServerId(server)

    await adapter.retain_if_changed(
        community_id=cid, server_id=sid, rel_path="server.properties"
    )
    # Change current/ at rest (write_file retains the prior + publishes new bytes),
    # then snapshot again: the new authoritative bytes differ from the newest
    # retained version, so a fresh version is retained.
    await adapter.write_file(
        community_id=cid,
        server_id=sid,
        rel_path="server.properties",
        content=b"motd=v2",
    )
    await adapter.retain_if_changed(
        community_id=cid, server_id=sid, rel_path="server.properties"
    )

    version_ids = await adapter.list_versions(
        community_id=cid, server_id=sid, rel_path="server.properties"
    )
    # The newest retained version is the now-current b"motd=v2" the second snapshot
    # captured (versions are newest-first), proving a changed copy is re-retained.
    newest = await storage.read_file_version(
        StorageCommunityId(community),
        StorageServerId(server),
        RelPath("server.properties"),
        VersionId(version_ids[0]),
    )
    assert newest == b"motd=v2"


async def test_retain_if_changed_missing_file_is_noop(tmp_path: Path) -> None:
    # A file created while running has no authoritative copy yet, so retaining is a
    # no-op (no error, no version) — the edit proceeds unversioned (#351/#344).
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)
    cid, sid = CommunityId(community), ServerId(server)

    await adapter.retain_if_changed(
        community_id=cid, server_id=sid, rel_path="created-while-running.txt"
    )
    versions = await adapter.list_versions(
        community_id=cid, server_id=sid, rel_path="created-while-running.txt"
    )
    assert versions == []


async def test_retain_if_changed_traversal_is_invalid_path(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)

    with pytest.raises(InvalidFilePathError):
        await adapter.retain_if_changed(
            community_id=CommunityId(community),
            server_id=ServerId(server),
            rel_path="../escape",
        )


async def test_delete_missing_file_is_file_not_found(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)

    with pytest.raises(ServerFileNotFoundError):
        await adapter.delete_file(
            community_id=CommunityId(community),
            server_id=ServerId(server),
            rel_path="nope.txt",
        )


async def test_delete_dir_removes_subtree(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await publish(
        storage,
        StorageCommunityId(community),
        StorageServerId(server),
        {"world/level.dat": b"a", "world/region/r.dat": b"b", "keep.txt": b"k"},
    )
    adapter = StorageFileStoreAdapter(storage=storage)
    cid, sid = CommunityId(community), ServerId(server)

    await adapter.delete_dir(community_id=cid, server_id=sid, rel_path="world")

    with pytest.raises(ServerFileNotFoundError):
        await adapter.list_dir(community_id=cid, server_id=sid, rel_path="world")
    assert (
        await adapter.read_file(community_id=cid, server_id=sid, rel_path="keep.txt")
        == b"k"
    )


async def test_make_dir_creates_empty_directory(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)
    cid, sid = CommunityId(community), ServerId(server)

    await adapter.make_dir(community_id=cid, server_id=sid, rel_path="plugins")
    entries = await adapter.list_dir(
        community_id=cid, server_id=sid, rel_path="plugins"
    )
    assert entries == []


async def test_delete_traversal_is_invalid_path(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)

    with pytest.raises(InvalidFilePathError):
        await adapter.delete_file(
            community_id=CommunityId(community),
            server_id=ServerId(server),
            rel_path="../escape",
        )


async def test_read_missing_is_file_not_found(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)

    with pytest.raises(ServerFileNotFoundError):
        await adapter.read_file(
            community_id=CommunityId(community),
            server_id=ServerId(server),
            rel_path="nope.txt",
        )


async def test_read_traversal_is_invalid_path(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)

    for bad in ("../escape", "/etc/passwd", "a/../../escape"):
        with pytest.raises(InvalidFilePathError):
            await adapter.read_file(
                community_id=CommunityId(community),
                server_id=ServerId(server),
                rel_path=bad,
            )


def test_validate_rel_path_rejects_traversal(tmp_path: Path) -> None:
    # The running branch pre-rejects through the seam (no Storage I/O); the
    # adapter applies the storage string-level rule, keeping RelPath behind the
    # seam (issue #122).
    adapter = StorageFileStoreAdapter(storage=FsStorage(tmp_path))

    for bad in ("../escape", "/etc/passwd", "a/../../escape"):
        with pytest.raises(InvalidFilePathError):
            adapter.validate_rel_path(bad)


async def test_rename_file_moves_without_version_capture(tmp_path: Path) -> None:
    """rename_file moves a file atomically without version retention (#1164)."""
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await publish(
        storage,
        StorageCommunityId(community),
        StorageServerId(server),
        {"mods/test.jar": b"jar-bytes", "server.properties": b"keep"},
    )
    adapter = StorageFileStoreAdapter(storage=storage)
    cid, sid = CommunityId(community), ServerId(server)

    await adapter.rename_file(
        community_id=cid,
        server_id=sid,
        from_path="mods/test.jar",
        to_path="mods/test.jar.disabled",
    )

    # The file is at the new path.
    out = await adapter.read_file(
        community_id=cid, server_id=sid, rel_path="mods/test.jar.disabled"
    )
    assert out == b"jar-bytes"
    # The old path is gone.
    with pytest.raises(ServerFileNotFoundError):
        await adapter.read_file(
            community_id=cid, server_id=sid, rel_path="mods/test.jar"
        )
    # No version was captured on either path.
    versions_old = await adapter.list_versions(
        community_id=cid, server_id=sid, rel_path="mods/test.jar"
    )
    assert versions_old == []
    versions_new = await adapter.list_versions(
        community_id=cid, server_id=sid, rel_path="mods/test.jar.disabled"
    )
    assert versions_new == []


async def test_rename_file_missing_is_file_not_found(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)

    with pytest.raises(ServerFileNotFoundError):
        await adapter.rename_file(
            community_id=CommunityId(community),
            server_id=ServerId(server),
            from_path="nope.txt",
            to_path="dest.txt",
        )


async def test_rename_file_traversal_is_invalid_path(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = _scope()
    await _seed(storage, community, server)
    adapter = StorageFileStoreAdapter(storage=storage)

    with pytest.raises(InvalidFilePathError):
        await adapter.rename_file(
            community_id=CommunityId(community),
            server_id=ServerId(server),
            from_path="../escape",
            to_path="dest.txt",
        )


def test_validate_rel_path_accepts_clean_path(tmp_path: Path) -> None:
    adapter = StorageFileStoreAdapter(storage=FsStorage(tmp_path))

    adapter.validate_rel_path("world/level.dat")
