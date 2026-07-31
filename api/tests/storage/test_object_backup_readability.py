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

import io
import tarfile
from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.storage.adapters import object_store
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


@pytest.fixture(autouse=True)
def _no_reprobe_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the re-read backoff to zero (it is a production pacing detail, not
    behaviour under test, and the failure-path cases would otherwise each pay it)."""

    monkeypatch.setattr(object_store, "_REPROBE_BACKOFF_S", 0)


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
    # Each get_object attempt pops one entry, so a drained queue proves the verdict
    # took TWO reads: a body that ends early is re-read like a transport teardown
    # rather than condemned on the first attempt. Condemning it immediately would
    # still raise below, so without this the symmetry is untested.
    store.read_aborts[object_key] = [None, None]

    with pytest.raises(ArchiveUnreadableError):
        await storage.check_backup_health(community, server, key)

    assert store.read_aborts[object_key] == []


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


def _restore_accepts(archive: bytes) -> bool:
    """Does the restore/upload path accept these bytes? (``tarfile.open(r:gz)``.)

    The probe must never condemn an archive this returns ``True`` for: a false
    "unhealthy" quarantines a restorable backup and emits a spurious audit entry,
    which is the same class of defect as the false "healthy" this issue is about.
    """

    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            tar.getmembers()
    except Exception:
        return False
    return True


@pytest.mark.parametrize(
    ("label", "suffix"),
    [
        # gzip permits members to be appended, and the shell tools do it.
        ("concatenated second member", region_targz({"world/region/r.0.1.mca": b"y"})),
        # Padding after a complete member: restore stops at the tar EOF marker and
        # never reads this far, so it restores fine (measured, issue #2371 review).
        ("trailing zero padding", b"\x00" * 1024),
        ("trailing junk", b"not-a-gzip-member"),
    ],
    ids=["concatenated", "zero-padding", "junk"],
)
async def test_bytes_after_a_complete_member_stay_healthy(
    label: str, suffix: bytes
) -> None:
    """Whatever follows a COMPLETE gzip member cannot make the archive unreadable —
    the restore path accepts all of these, so the probe must too."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    archive = _sound_archive()
    key = await _put_backup(storage, community, server, archive)
    object_key = storage._backup_key(community, server, key)
    store.objects[object_key] = archive + suffix

    # The precondition that makes this a false-quarantine test rather than a
    # preference: restore really does accept these bytes.
    assert _restore_accepts(archive + suffix), label

    report = await storage.check_backup_health(community, server, key)

    assert report.healthy


def _damaged_second_member() -> bytes:
    second = bytearray(region_targz({"world/region/r.0.1.mca": b"y" * 64}))
    second[-5] ^= 0xFF  # a real member header, a trailer that no longer matches.
    return bytes(second)


@pytest.mark.parametrize(
    "suffix",
    [
        _damaged_second_member(),
        # Nothing but the magic: a member that announces itself and then stops. This
        # is the documented divergence from ``tarfile``, which accepts it because it
        # never reads past the tar end-of-archive marker.
        b"\x1f\x8b",
    ],
    ids=["damaged-member", "header-only"],
)
async def test_a_damaged_second_member_is_still_unreadable(suffix: bytes) -> None:
    """Leniency stops at the gzip magic: bytes that announce themselves as another
    member must actually be one, or the archive is damaged."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    archive = _sound_archive()
    key = await _put_backup(storage, community, server, archive)
    object_key = storage._backup_key(community, server, key)
    store.objects[object_key] = archive + suffix

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

    with pytest.raises(ObjectStoreUnavailableError) as excinfo:
        await storage.check_backup_health(community, server, key)

    # The transport error that actually failed the read stays chained, so the
    # traceback the sweep logs reaches the root cause instead of stopping at this
    # summary — the object client translates every body-read failure, so this chain
    # is the only place the underlying aiohttp/botocore error survives.
    assert isinstance(excinfo.value.__cause__, ObjectStoreUnavailableError)


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
