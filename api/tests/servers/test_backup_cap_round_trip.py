"""A backup the platform writes at the decompressed-size cap round-trips (#3859).

Restore, the health check and the upload gate count the same quantity toward
the same cap: every byte of the archive's decompressed stream. The backup
writers count it too, as they write, and write no backup past it. So a backup
written at the cap restores, is healthy and uploads again, and one byte less is
refused by all four. Each test runs on both backends, under a cap taken from
the stream the writer writes for a small working set.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from mc_server_dashboard_api.servers.application.backups import (
    _validate_backup_archive,
)
from mc_server_dashboard_api.servers.application.files import (
    MAX_ARCHIVE_ENTRIES,
    MAX_DECOMPRESSED_BYTES,
)
from mc_server_dashboard_api.servers.domain.errors import InvalidBackupArchiveError
from mc_server_dashboard_api.storage.adapters import fs, object_store
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.errors import ArchiveTooLargeError
from mc_server_dashboard_api.storage.domain.port import Storage
from mc_server_dashboard_api.storage.domain.value_objects import (
    BackupKey,
    CommunityId,
    ServerId,
)
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory
from tests.storage.helpers import drain, new_scope, read_tar, stream_of, tar_stream

_FILES = {"server.properties": b"k=v", "world/level.dat": b"w" * 30_000}


class _Store:
    """One backend's store, opened under any cap."""

    def __init__(self, backend: str, root: Path) -> None:
        self._backend = backend
        self._root = root
        self._s3 = FakeS3Store()

    def under(self, cap: int) -> Storage:
        if self._backend == "fs":
            return FsStorage(self._root, max_restore_bytes=cap)
        return ObjectStorage(fake_s3_factory(self._s3), max_restore_bytes=cap)


@pytest.fixture(params=["fs", "object"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> _Store:
    return _Store(request.param, tmp_path)


async def _backup_at_the_cap(
    store: _Store,
) -> tuple[CommunityId, ServerId, BackupKey, bytes, int]:
    """Publish :data:`_FILES` and back them up: the scope, the backup, its
    archive, and the length of its decompressed stream, which is the cap."""

    storage = store.under(1 << 30)
    community, server = new_scope()
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(_FILES))
    await storage.commit_snapshot(handle)
    key = await storage.create_backup_from_current(community, server)
    archive = await drain(storage.open_backup(community, server, key))
    return community, server, key, archive, len(gzip.decompress(archive))


async def test_a_backup_written_at_the_cap_restores_and_uploads_again(
    store: _Store,
) -> None:
    community, server, _, _, cap = await _backup_at_the_cap(store)
    storage = store.under(cap)

    key = await storage.create_backup_from_current(community, server)
    archive = await drain(storage.open_backup(community, server, key))

    assert len(gzip.decompress(archive)) == cap
    assert (await storage.check_backup_health(community, server, key)).healthy
    await storage.restore_backup(community, server, key)
    assert read_tar(await drain(storage.open_hydrate_source(community, server))) == (
        _FILES
    )
    _validate_backup_archive(
        archive, max_entries=MAX_ARCHIVE_ENTRIES, max_decompressed_bytes=cap
    )


async def test_one_byte_under_the_cap_refuses_it_everywhere(store: _Store) -> None:
    community, server, _, archive, cap = await _backup_at_the_cap(store)
    storage = store.under(cap - 1)

    # The writer writes no such backup...
    with pytest.raises(ArchiveTooLargeError):
        await storage.create_backup_from_current(community, server)
    # ...and restore, the health check and the upload gate each refuse one.
    key = await storage.put_backup(community, server, stream_of(archive, chunk=8192))
    with pytest.raises(ArchiveTooLargeError):
        await storage.restore_backup(community, server, key)
    with pytest.raises(ArchiveTooLargeError):
        await storage.check_backup_health(community, server, key)
    with pytest.raises(InvalidBackupArchiveError, match="decompressed size"):
        _validate_backup_archive(
            archive, max_entries=MAX_ARCHIVE_ENTRIES, max_decompressed_bytes=cap - 1
        )


def test_the_upload_gate_and_both_backends_hold_backups_to_one_cap() -> None:
    # What the tests above show under an injected cap holds in production only
    # while the gate's cap is the one both backends restore and write under.
    assert (
        MAX_DECOMPRESSED_BYTES
        == fs._DEFAULT_MAX_RESTORE_BYTES
        == object_store._DEFAULT_MAX_RESTORE_BYTES
    )
