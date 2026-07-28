"""The object backend's backup readability probe (issue #2371).

``ObjectStorage.check_backup_health`` used to confirm the object EXISTS with a
``HEAD`` and report healthy without reading a byte, so an archive whose bytes the
store can no longer produce — unreadable, and therefore unrestorable — was
indistinguishable from a good one. The probe now streams the stored object end to
end, checking BOTH that the delivered byte count matches the length ``HEAD``
declares AND that the bytes decompress as a gzip stream terminating at a
well-formed trailer (so silent bit-rot is caught, not only truncation). The
decompressed output is discarded as it is produced — nothing is staged or buffered.

The delicate case is telling *damage* from an *outage*: the reported defect
surfaces as a connection teardown mid-body, exactly like a store having a bad
minute. The probe re-reads after a teardown and only calls the archive unreadable
when the body reproducibly ends at the same non-zero offset; anything else stays
an availability failure, so a sweep run during an outage cannot quarantine every
backup in the deployment.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.errors import (
    ArchiveUnreadableError,
    NotFoundError,
    ObjectStoreUnavailableError,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    BackupKey,
    CommunityId,
    ServerId,
)
from tests.storage.fake_s3 import FakeS3Store, close_tracking_factory, fake_s3_factory
from tests.storage.helpers import healthy_region_bytes, new_scope, region_targz


def _store_and_storage() -> tuple[FakeS3Store, ObjectStorage]:
    store = FakeS3Store()
    return store, ObjectStorage(close_tracking_factory(fake_s3_factory(store)))


async def _put_backup(
    storage: ObjectStorage,
    community: CommunityId,
    server: ServerId,
    archive: bytes,
) -> BackupKey:
    async def _stream() -> AsyncIterator[bytes]:
        yield archive

    return await storage.put_backup(community, server, _stream())


def _sound_archive() -> bytes:
    return region_targz({"world/region/r.0.0.mca": healthy_region_bytes()})


async def test_sound_archive_reads_back_healthy() -> None:
    store, storage = _store_and_storage()
    community, server = new_scope()
    key = await _put_backup(storage, community, server, _sound_archive())

    report = await storage.check_backup_health(community, server, key)

    assert report.healthy
    assert store.objects  # the archive is still in place: the probe is read-only.


async def test_unknown_key_raises_not_found() -> None:
    _store, storage = _store_and_storage()
    community, server = new_scope()

    with pytest.raises(NotFoundError):
        await storage.check_backup_health(community, server, BackupKey("missing"))


async def test_body_short_of_the_declared_length_is_unreadable() -> None:
    """The reported deployment signature: ``HEAD`` declares the full archive length
    but the store can only produce a prefix of it."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    archive = _sound_archive()
    key = await _put_backup(storage, community, server, archive)
    object_key = storage._backup_key(community, server, key)
    # The stored body is a clean prefix; HEAD keeps declaring the original length.
    store.objects[object_key] = archive[: len(archive) // 2]
    store.declared_sizes[object_key] = len(archive)

    with pytest.raises(ArchiveUnreadableError):
        await storage.check_backup_health(community, server, key)


async def test_truncated_body_with_a_matching_declared_length_is_unreadable() -> None:
    """A length comparison alone cannot catch this: the store declares exactly what
    it delivers, and only walking the gzip stream shows it never terminates."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    archive = _sound_archive()
    key = await _put_backup(storage, community, server, archive)
    object_key = storage._backup_key(community, server, key)
    store.objects[object_key] = archive[: len(archive) // 2]

    with pytest.raises(ArchiveUnreadableError):
        await storage.check_backup_health(community, server, key)


async def test_corrupt_gzip_trailer_is_unreadable() -> None:
    """Silent bit-rot: every declared byte is delivered, but the gzip trailer's
    integrity fields no longer describe the payload."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    archive = _sound_archive()
    key = await _put_backup(storage, community, server, archive)
    object_key = storage._backup_key(community, server, key)
    rotted = bytearray(archive)
    rotted[-5] ^= 0xFF  # inside the CRC32 + ISIZE trailer; the length is unchanged.
    store.objects[object_key] = bytes(rotted)

    with pytest.raises(ArchiveUnreadableError):
        await storage.check_backup_health(community, server, key)


async def test_reproducible_mid_stream_teardown_is_unreadable() -> None:
    """The reported defect: the transfer runs at full speed to a fixed offset, then
    the connection aborts — identically on every attempt. Persistent damage."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    archive = _sound_archive()
    key = await _put_backup(storage, community, server, archive)
    object_key = storage._backup_key(community, server, key)
    cut = len(archive) // 2
    store.read_aborts[object_key] = [cut, cut]

    with pytest.raises(ArchiveUnreadableError):
        await storage.check_backup_health(community, server, key)


async def test_teardown_at_a_shifting_offset_reports_the_store_unavailable() -> None:
    """A store having a bad minute tears the body down wherever it happens to be.
    That must stay an availability failure — quarantining on it would condemn every
    backup in the deployment on one outage."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    archive = _sound_archive()
    key = await _put_backup(storage, community, server, archive)
    object_key = storage._backup_key(community, server, key)
    store.read_aborts[object_key] = [len(archive) // 2, len(archive) // 3]

    with pytest.raises(ObjectStoreUnavailableError):
        await storage.check_backup_health(community, server, key)


async def test_teardown_before_any_byte_reports_the_store_unavailable() -> None:
    """A store refusing the read outright delivers nothing, reproducibly — that is
    the outage signature, not evidence about this object's bytes."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    archive = _sound_archive()
    key = await _put_backup(storage, community, server, archive)
    object_key = storage._backup_key(community, server, key)
    store.read_aborts[object_key] = [0, 0]

    with pytest.raises(ObjectStoreUnavailableError):
        await storage.check_backup_health(community, server, key)


async def test_transient_teardown_that_does_not_recur_reads_back_healthy() -> None:
    """One torn-down read followed by a complete one is a blip, not damage."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    archive = _sound_archive()
    key = await _put_backup(storage, community, server, archive)
    object_key = storage._backup_key(community, server, key)
    store.read_aborts[object_key] = [len(archive) // 2]

    report = await storage.check_backup_health(community, server, key)

    assert report.healthy


async def test_a_sound_archive_is_read_only_once() -> None:
    """The re-read exists only to classify a teardown: a sound archive costs exactly
    one full read per sweep, not two."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    key = await _put_backup(storage, community, server, _sound_archive())
    object_key = storage._backup_key(community, server, key)
    # Each ``get_object`` attempt pops one entry off the queue, so what is left
    # afterwards counts the reads: two queued "deliver in full" entries, one left.
    store.read_aborts[object_key] = [None, None]

    await storage.check_backup_health(community, server, key)

    assert store.read_aborts[object_key] == [None]
