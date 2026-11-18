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
minute. The probe re-reads after a teardown: a complete re-read is healthy, and
the archive is called unreadable when both reads delivered bytes and both ended
short — at the further of the two offsets, since an RST-torn read loses a
timing-dependent tail (#2381) — or when neither read delivered a byte at all
(#2379). A read that delivered nothing never corroborates a short one: that pair
stays an availability failure.
"""

from __future__ import annotations

import gzip
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
from mc_server_dashboard_api.storage.integrity.archive import GzipReadProbe
from tests import gzip_shapes
from tests.storage.fake_s3 import FakeS3Store, close_tracking_factory, fake_s3_factory
from tests.storage.helpers import (
    healthy_region_bytes,
    new_scope,
    region_targz,
    tar_bytes,
)


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


_SOUND_TAR = tar_bytes({"world/region/r.0.0.mca": healthy_region_bytes()})


@pytest.mark.parametrize(
    "archive",
    [
        # gzip permits members to be appended, and the shell tools do it: the tar
        # goes on in the second member.
        gzip.compress(_SOUND_TAR[:1024]) + gzip.compress(_SOUND_TAR[1024:]),
        # Zero padding after the gzip stream, which gzip reads past.
        _sound_archive() + b"\x00" * 1024,
    ],
    ids=["concatenated", "zero-padding"],
)
async def test_a_gzip_stream_restore_reads_whole_stays_healthy(archive: bytes) -> None:
    """A gzip stream of several members, or one with zero padding after it, is
    healthy: restore reads it whole (issue #3798), so the probe must too. A false
    "unhealthy" quarantines a restorable backup and emits a spurious audit entry,
    which is the same class of defect as the false "healthy" this issue is about."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    key = await _put_backup(storage, community, server, archive)

    report = await storage.check_backup_health(community, server, key)

    assert report.healthy
    # The precondition that makes this a false-quarantine test rather than a
    # preference: restore really does accept these bytes.
    await storage.restore_backup(community, server, key)


def _damaged_second_member() -> bytes:
    second = bytearray(region_targz({"world/region/r.0.1.mca": b"y" * 64}))
    second[-5] ^= 0xFF  # a real member header, a trailer that no longer matches.
    return bytes(second)


@pytest.mark.parametrize(
    "suffix",
    [
        _damaged_second_member(),
        # Nothing but the magic: a member that announces itself and then stops.
        b"\x1f\x8b",
        # Bytes that are not a gzip member at all.
        b"not-a-gzip-member",
        # A second tar.gz: a sound gzip member, but a second tar after the first
        # one's end-of-archive marker.
        region_targz({"world/region/r.0.1.mca": b"y"}),
    ],
    ids=["damaged-member", "header-only", "junk", "second-tar"],
)
async def test_bytes_after_the_archive_restore_refuses_are_unreadable(
    suffix: bytes,
) -> None:
    """Restore reads the archive to its gzip end (issue #3798): bytes after the
    gzip stream must be another sound gzip member or zero padding, and only
    zeros may follow the tar's end-of-archive marker. So these are unreadable to
    the probe as they are to restore (issue #3236)."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    archive = _sound_archive()
    key = await _put_backup(storage, community, server, archive)
    object_key = storage._backup_key(community, server, key)
    store.objects[object_key] = archive + suffix

    with pytest.raises(ArchiveUnreadableError):
        await storage.check_backup_health(community, server, key)
    with pytest.raises(ArchiveUnreadableError):
        await storage.restore_backup(community, server, key)


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


def test_the_probe_refuses_no_part_of_a_sound_stream_before_its_end() -> None:
    """The store delivers an archive in parts that may end anywhere: inside a
    member header, inside the zero padding after a member, on the first byte of
    the next member's magic. The probe refuses only what no further byte could
    make sound, so a body that ends short reaches the re-read (issue #2371);
    its end is judged by ``finish`` (issue #3835)."""

    tar = tar_bytes({"server.properties": b"k=v"})
    first, second = gzip_shapes.in_members(tar, [len(tar) // 2])
    body = first + bytes(512) + second + bytes(1024)

    for at in range(len(body) + 1):
        probe = GzipReadProbe()
        probe.feed(body[:at])
        probe.feed(body[at:])
        probe.finish()


def test_the_probe_refuses_more_than_10000_gzip_members_as_they_arrive() -> None:
    """The probe reads the parts the store delivers on the event loop, so it
    stops at the 10,001st gzip member rather than walking every member of the
    part (issue #3835)."""

    body = gzip_shapes.refused(tar_bytes({"server.properties": b"k=v"}))[
        "over-the-member-cap"
    ]
    probe = GzipReadProbe()

    with pytest.raises(ArchiveUnreadableError, match="more than 10000 gzip members"):
        probe.feed(body)


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

    with pytest.raises(ArchiveUnreadableError) as excinfo:
        await storage.check_backup_health(community, server, key)

    assert f"past {cut} of the {len(archive)} declared" in str(excinfo.value)


@pytest.mark.parametrize("shorter_first", [True, False], ids=["first", "second"])
async def test_reads_ending_short_at_different_offsets_are_unreadable(
    shorter_first: bool,
) -> None:
    """Issue #2381: a connection torn down with an RST discards whatever was still in
    flight, and how much is timing-dependent — so two reads of the same damaged body
    stop at different offsets. An RST only ever loses bytes, never invents them:
    neither read got past the cut, and the further one is the better estimate of it.
    Disagreement between two short reads is therefore not an outage."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    archive = _sound_archive()
    key = await _put_backup(storage, community, server, archive)
    object_key = storage._backup_key(community, server, key)
    further, shorter = len(archive) // 2, len(archive) // 3
    store.read_aborts[object_key] = (
        [shorter, further] if shorter_first else [further, shorter]
    )

    with pytest.raises(ArchiveUnreadableError) as excinfo:
        await storage.check_backup_health(community, server, key)

    # The reported end is the maximum of the two, whichever read reached it.
    assert f"past {further} of the {len(archive)} declared" in str(excinfo.value)


@pytest.mark.parametrize("empty_first", [True, False], ids=["first", "second"])
async def test_a_read_that_delivers_nothing_cannot_corroborate_a_short_one(
    empty_first: bool,
) -> None:
    """A read that delivered no byte never reached the body — it is the store
    refusing, which says nothing about where THIS object's bytes end. Paired with a
    short read it leaves one observation of the body, which is exactly the ambiguity
    the re-read exists to resolve, so it stays an availability failure: taking the
    maximum here would mark a backup UNREADABLE because the store went down
    mid-read."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    archive = _sound_archive()
    key = await _put_backup(storage, community, server, archive)
    object_key = storage._backup_key(community, server, key)
    cut = len(archive) // 2
    store.read_aborts[object_key] = [0, cut] if empty_first else [cut, 0]

    with pytest.raises(ObjectStoreUnavailableError) as excinfo:
        await storage.check_backup_health(community, server, key)

    # The transport error that actually failed the read stays chained, so the
    # traceback the sweep logs reaches the root cause instead of stopping at this
    # summary — the object client translates every body-read failure, so this chain
    # is the only place the underlying aiohttp/botocore error survives.
    assert isinstance(excinfo.value.__cause__, ObjectStoreUnavailableError)


async def test_reads_that_both_deliver_nothing_are_unreadable() -> None:
    """Issue #2379: an object whose every read delivers no byte is dead, not an
    outage. The store answered ``HEAD`` for it moments earlier — a store that is
    down fails there first — and treating it as an outage aborted every sweep pass
    on this one object, so no other backup was ever checked again."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    archive = _sound_archive()
    key = await _put_backup(storage, community, server, archive)
    object_key = storage._backup_key(community, server, key)
    store.read_aborts[object_key] = [0, 0]

    with pytest.raises(ArchiveUnreadableError) as excinfo:
        await storage.check_backup_health(community, server, key)

    assert f"past 0 of the {len(archive)} declared" in str(excinfo.value)


async def test_a_zero_delivery_verdict_is_not_sticky() -> None:
    """The verdict is revisable (issue #2379): it is re-derived from the reads on
    every probe, so once the store delivers the archive again it reads back healthy.
    A half-up backend can make an intact archive deliver nothing, and that must not
    leave it condemned."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    key = await _put_backup(storage, community, server, _sound_archive())
    object_key = storage._backup_key(community, server, key)
    store.read_aborts[object_key] = [0, 0]
    with pytest.raises(ArchiveUnreadableError):
        await storage.check_backup_health(community, server, key)

    report = await storage.check_backup_health(community, server, key)

    assert report.healthy


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


# --- restore: the same damage-vs-outage line (issue #3230) -------------------


async def test_restore_of_a_body_short_of_its_declared_length_is_an_outage() -> None:
    """A restore spools the archive with ONE read, so a body that ends cleanly short
    of what ``HEAD`` declares is the single ambiguous observation the probe above
    re-reads to classify. Restore does not condemn on it: it refuses as an outage
    (no verdict about the archive) rather than reading the short spool and calling
    the archive unreadable."""

    store, storage = _store_and_storage()
    community, server = new_scope()
    archive = _sound_archive()
    key = await _put_backup(storage, community, server, archive)
    object_key = storage._backup_key(community, server, key)
    store.objects[object_key] = archive[: len(archive) // 2]
    store.declared_sizes[object_key] = len(archive)

    with pytest.raises(ObjectStoreUnavailableError):
        await storage.restore_backup(community, server, key)
