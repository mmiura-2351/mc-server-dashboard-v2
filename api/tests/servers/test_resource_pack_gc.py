"""Resource-pack orphan GC (issue #3277).

The pack row is the source of truth, so a stored blob no row names is never
served -- and nothing removed it. Each orphan here is made the way production
makes it, by the use case that leaves it behind, and stored through the real
:class:`ObjectResourcePackStore` over the in-memory S3 stub, whose store clock
stamps each blob with the time the GC's safety window reads.

The two guards are pinned the same way: an upload in flight has stored its blob
but not yet committed its row, and is spared by the safety window; a row that
commits after the GC read the rows is caught by the re-read just before the
delete.

A failing delete is the stub's outage on that one pack's key, so it raises what
production raises (issue #3871).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
import uuid
import zipfile
from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.gc_pass import ResumePoint
from mc_server_dashboard_api.logging import TextFormatter
from mc_server_dashboard_api.servers.adapters.resource_pack_store import (
    ObjectResourcePackStore,
)
from mc_server_dashboard_api.servers.application.resource_pack_gc import (
    GC_SAFETY_WINDOW,
    ResourcePackGcResult,
    RunResourcePackGc,
)
from mc_server_dashboard_api.servers.application.resource_packs import (
    DeleteResourcePack,
    UploadResourcePack,
)
from mc_server_dashboard_api.servers.domain.errors import (
    ResourcePackStorageUnavailableError,
    ResourcePackStorageUnreachableError,
)
from mc_server_dashboard_api.servers.domain.resource_pack import (
    ResourcePack,
    ResourcePackId,
)
from mc_server_dashboard_api.storage.domain.errors import ObjectStoreUnreachableError
from tests.servers.fakes import FakeClock, FakeResourcePackRepository, FakeUnitOfWork
from tests.storage.fake_s3 import FakeS3Store
from tests.storage.faulty_s3 import Faults, faulty_s3_factory
from tests.store_ownership import FakeStoreOwnership

_T0 = dt.datetime(2026, 6, 20, 12, 0, tzinfo=dt.UTC)
_FILENAME = "pack.zip"


def _resume_point() -> ResumePoint:
    """A resume point whose pass without a mark starts at the first key in key
    order, so a pass meets the keys in the order a test seeds them (issue #3934)."""

    return ResumePoint(start=lambda count: 0)


def _pack_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pack.mcmeta", json.dumps({"pack": {"pack_format": 15}}))
    return buf.getvalue()


class _Bucket:
    """The bucket, the store over it, and the one clock both sides read."""

    def __init__(self) -> None:
        self.clock = FakeClock(_T0)
        self.backing = FakeS3Store(clock=self.clock.now)
        self.faults = Faults()
        self.store = ObjectResourcePackStore(
            faulty_s3_factory(self.backing, self.faults)
        )
        self.rows = FakeResourcePackRepository()
        # The GC's one resume point, which every pass below shares, as the
        # loop's passes do (issue #3934).
        self.resume_point = _resume_point()

    def blob_keys(self) -> list[str]:
        return [
            key for key in self.backing.objects if key.startswith("resource-packs/")
        ]

    def answer_no_delete(self) -> list[str]:
        """Give no answer to any delete; the keys they were for, as they come."""

        attempted: list[str] = []

        def _fails(op: str, key: str) -> bool:
            if op != "delete_object":
                return False
            attempted.append(key)
            return True

        self.faults.when = _fails
        self.faults.error = ObjectStoreUnreachableError
        return attempted

    async def upload(self, uow: FakeUnitOfWork | None = None) -> ResourcePack:
        return await UploadResourcePack(
            uow=uow or FakeUnitOfWork(resource_packs=self.rows),
            store=self.store,
            clock=self.clock,
        )(
            filename=_FILENAME,
            display_name="Pack",
            content=_pack_zip(),
            uploaded_by=uuid.uuid4(),
        )

    async def orphans(self, count: int, filename: str = _FILENAME) -> list[str]:
        """Store *count* blobs no row names; their keys, in the order a pass meets
        them."""

        async def _body() -> AsyncIterator[bytes]:
            yield _pack_zip()

        for _ in range(count):
            await self.store.put(ResourcePackId(uuid.uuid4()), filename, _body())
        return sorted(self.blob_keys())

    async def gc(
        self, ownership: FakeStoreOwnership | None = None
    ) -> ResourcePackGcResult:
        """Run one pass once every blob stored so far is past the safety window."""

        self.clock.set(self.clock.now() + GC_SAFETY_WINDOW + dt.timedelta(minutes=1))
        return await RunResourcePackGc(
            uow=FakeUnitOfWork(resource_packs=self.rows),
            store=self.store,
            clock=self.clock,
            ownership=ownership or FakeStoreOwnership(),
            resume_point=self.resume_point,
        )()


class _CommitFailsUnitOfWork(FakeUnitOfWork):
    """A unit of work whose commit fails, so the INSERT it staged never lands."""

    async def commit(self) -> None:
        # The fake keys a row in on ``add``; a failed commit leaves no row.
        self.resource_packs.packs.clear()
        raise RuntimeError("commit failed")


async def test_reclaims_the_blob_a_failed_delete_left() -> None:
    # DeleteResourcePack commits the row's delete first and removes the blob
    # best-effort (issue #1962): a store outage there answers 204 and leaves the
    # blob, and a repeated DELETE is a 404 that never reaches the store.
    bucket = _Bucket()
    pack = await bucket.upload()
    bucket.faults.when = lambda op, key: op == "delete_object"
    await DeleteResourcePack(
        uow=FakeUnitOfWork(resource_packs=bucket.rows), store=bucket.store
    )(resource_pack_id=pack.id, caller_id=pack.uploaded_by, is_platform_admin=False)
    assert bucket.rows.packs == {}
    assert bucket.blob_keys() != []
    bucket.faults.clear()

    await bucket.gc()

    assert bucket.blob_keys() == []


async def test_reclaims_the_blob_a_failed_commit_left() -> None:
    # UploadResourcePack stores the blob before it commits the row (issue #2458).
    bucket = _Bucket()
    with pytest.raises(RuntimeError, match="commit failed"):
        await bucket.upload(_CommitFailsUnitOfWork(resource_packs=bucket.rows))
    assert bucket.rows.packs == {}
    assert bucket.blob_keys() != []

    await bucket.gc()

    assert bucket.blob_keys() == []


async def test_reclaims_the_blob_an_unacknowledged_upload_left() -> None:
    # The store completed the upload and the response was lost: the upload answers
    # 503 with no row, and the completed blob stays (issue #2458).
    bucket = _Bucket()
    bucket.faults.lose_response = lambda op, key: op == "upload_multipart"
    with pytest.raises(ResourcePackStorageUnavailableError):
        await bucket.upload()
    bucket.faults.clear()
    assert bucket.blob_keys() != []

    await bucket.gc()

    assert bucket.blob_keys() == []


async def test_logs_each_reclaimed_blob_and_the_pass_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A reclaimed blob is the only copy of a user upload, so each one is logged
    # with what an operator needs to trace it: the pack id, the key and its age.
    bucket = _Bucket()
    with pytest.raises(RuntimeError, match="commit failed"):
        await bucket.upload(_CommitFailsUnitOfWork(resource_packs=bucket.rows))
    (orphan_key,) = bucket.blob_keys()
    await bucket.upload()

    with caplog.at_level(logging.INFO, logger="mc_server_dashboard_api"):
        await bucket.gc()

    size = len(_pack_zip())
    messages = [record.getMessage() for record in caplog.records]
    (reclaimed,) = [message for message in messages if orphan_key in message]
    assert orphan_key.split("/")[1] in reclaimed
    assert f"{size} bytes" in reclaimed
    assert "1:01:00" in reclaimed
    summary = messages[-1]
    for part in ("2 blob(s) listed", f"1 reclaimed ({size} bytes)", "0 failed", "in "):
        assert part in summary
    assert caplog.records[-1].levelno == logging.INFO


async def test_a_pass_that_reclaims_nothing_logs_its_summary_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A daily pass over packs that all have rows is the common case; it must not
    # leave an INFO line every time (issue #3871).
    bucket = _Bucket()
    await bucket.upload()

    with caplog.at_level(logging.DEBUG, logger="mc_server_dashboard_api"):
        await bucket.gc()

    assert [r for r in caplog.records if r.levelno >= logging.INFO] == []
    assert any("0 reclaimed" in r.getMessage() for r in caplog.records)


async def test_a_failing_delete_skips_that_pack_and_the_pass_reclaims_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # One pack whose delete keeps failing must not starve the ones after it
    # (issue #3871). It is the first the pass meets, so a pass that stopped at the
    # failure would reclaim nothing.
    bucket = _Bucket()
    failing, *others = await bucket.orphans(3)
    bucket.faults.when = lambda op, key: op == "delete_object" and key == failing

    with caplog.at_level(logging.INFO, logger="mc_server_dashboard_api"):
        result = await bucket.gc()

    assert bucket.blob_keys() == [failing]
    assert (result.scanned, result.deleted, result.failed) == (3, 2, 1)
    assert result.freed_bytes == len(_pack_zip()) * 2
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert failing.split("/")[1] in warning.getMessage()
    assert "ResourcePackStorageUnavailableError" in warning.getMessage()
    assert all(key.split("/")[1] not in warning.getMessage() for key in others)
    summary = caplog.records[-1]
    assert summary.levelno == logging.INFO
    assert "1 failed" in summary.getMessage()


async def test_a_pass_whose_every_delete_failed_logs_its_summary_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Nothing was reclaimed, but the pass is not empty: the summary is the one
    # line that counts the failures, so it stays visible (issue #3871).
    bucket = _Bucket()
    (failing,) = await bucket.orphans(1)
    bucket.faults.when = lambda op, key: op == "delete_object"

    with caplog.at_level(logging.DEBUG, logger="mc_server_dashboard_api"):
        result = await bucket.gc()

    assert bucket.blob_keys() == [failing]
    assert (result.deleted, result.failed) == (0, 1)
    (summary,) = [r for r in caplog.records if r.levelno == logging.INFO]
    assert "1 failed" in summary.getMessage()


async def test_five_failed_deletes_in_a_row_end_the_pass(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A store that fails every delete costs each one its retries and a WARNING
    # with a traceback. Five in a row end the pass, which still logs its summary,
    # at WARNING with the reason; the pack after them is not tried (issue #3906).
    bucket = _Bucket()
    keys = await bucket.orphans(6)
    failing = keys[:5]
    bucket.faults.when = lambda op, key: op == "delete_object" and key in failing

    with caplog.at_level(logging.DEBUG, logger="mc_server_dashboard_api"):
        result = await bucket.gc()

    assert sorted(bucket.blob_keys()) == keys
    assert (result.scanned, result.deleted, result.failed) == (6, 0, 5)
    summary = caplog.records[-1]
    assert summary.levelno == logging.WARNING
    assert vars(summary).get("reason") == "stopped_after_consecutive_failures"
    assert "6 blob(s) listed, 0 reclaimed (0 bytes), 5 failed" in summary.getMessage()


async def test_a_reclaimed_pack_restarts_the_count_of_failed_deletes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Four failures, one success, four more: never five in a row, so the pass
    # reaches every pack and ends as usual (issue #3906).
    bucket = _Bucket()
    keys = await bucket.orphans(9)
    reclaimed = keys.pop(4)
    bucket.faults.when = lambda op, key: op == "delete_object" and key in keys

    with caplog.at_level(logging.DEBUG, logger="mc_server_dashboard_api"):
        result = await bucket.gc()

    assert reclaimed not in bucket.blob_keys()
    assert sorted(bucket.blob_keys()) == keys
    assert (result.scanned, result.deleted, result.failed) == (9, 1, 8)
    summary = caplog.records[-1]
    assert summary.levelno == logging.INFO
    assert "reason" not in vars(summary)


# A pack stored before the upload refused such a name (issue #3907): logged raw,
# its newline would start a line that reads as a record of its own, and its
# escape sequence would clear the terminal tailing the log.
_FORGED_FILENAME = "x\nINFO mc_server_dashboard_api forged\x1b[2J.zip"


def _forged(rendered: str) -> bool:
    return "\x1b" in rendered or any(
        line.startswith("INFO mc_server_dashboard_api forged")
        for line in rendered.splitlines()
    )


async def test_logs_a_reclaimed_key_escaped(caplog: pytest.LogCaptureFixture) -> None:
    bucket = _Bucket()
    (key,) = await bucket.orphans(1, filename=_FORGED_FILENAME)

    with caplog.at_level(logging.INFO, logger="mc_server_dashboard_api"):
        await bucket.gc()

    assert bucket.blob_keys() == []
    rendered = [TextFormatter().format(record) for record in caplog.records]
    (reclaimed,) = [line for line in rendered if "reclaimed pack" in line]
    assert repr(key) in reclaimed
    assert not any(_forged(line) for line in rendered)


async def test_logs_a_failed_delete_key_escaped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bucket = _Bucket()
    (key,) = await bucket.orphans(1, filename=_FORGED_FILENAME)
    bucket.faults.when = lambda op, k: op == "delete_object"

    with caplog.at_level(logging.INFO, logger="mc_server_dashboard_api"):
        await bucket.gc()

    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    # Rendered whole, traceback included: the record is the text an operator reads.
    rendered = TextFormatter().format(warning)
    assert repr(key) in rendered.partition("\n")[0]
    assert not _forged(rendered)


@pytest.mark.parametrize("operation", ["list_objects", "delete_object"])
async def test_a_store_that_gives_no_answer_ends_the_pass(operation: str) -> None:
    # A delete the store never answered says nothing about this pack: every later
    # delete would wait out the same timeouts. The pass ends instead of skipping
    # each pack in turn (issue #3871). A pack's delete lists its prefix before it
    # deletes, so either call can be the one that gets no answer.
    bucket = _Bucket()
    keys = await bucket.orphans(2)
    prefix = keys[0].rpartition("/")[0]
    bucket.faults.when = lambda op, key: op == operation and key.startswith(prefix)
    bucket.faults.error = ObjectStoreUnreachableError

    with pytest.raises(ResourcePackStorageUnreachableError):
        await bucket.gc()

    assert sorted(bucket.blob_keys()) == keys


async def test_a_cancelled_delete_ends_the_pass() -> None:
    # Shutdown cancels the loop's task mid-pass; the cancellation must reach the
    # loop, not be counted as one failed pack (issue #3871).
    bucket = _Bucket()
    keys = await bucket.orphans(2)
    bucket.faults.when = lambda op, key: op == "delete_object" and key == keys[0]
    bucket.faults.error = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await bucket.gc()

    assert sorted(bucket.blob_keys()) == keys


async def test_keeps_the_blob_of_a_pack_with_a_row() -> None:
    bucket = _Bucket()
    pack = await bucket.upload()

    await bucket.gc()

    assert bucket.blob_keys() == [f"resource-packs/{pack.id.value}/{_FILENAME}"]


async def test_spares_the_blob_of_an_upload_in_flight() -> None:
    # Mid-upload: the blob is stored and the row not yet committed, exactly the
    # state of an orphan -- told apart only by its age. A pass that runs then must
    # leave the blob for the row about to name it.
    bucket = _Bucket()
    pack_id = ResourcePackId(uuid.uuid4())

    async def _body() -> AsyncIterator[bytes]:
        yield _pack_zip()

    await bucket.store.put(pack_id, _FILENAME, _body())
    bucket.clock.set(_T0 + GC_SAFETY_WINDOW - dt.timedelta(seconds=1))
    await RunResourcePackGc(
        uow=FakeUnitOfWork(resource_packs=bucket.rows),
        store=bucket.store,
        clock=bucket.clock,
        ownership=FakeStoreOwnership(),
        resume_point=_resume_point(),
    )()

    assert bucket.blob_keys() == [f"resource-packs/{pack_id.value}/{_FILENAME}"]


class _RowCommitsAfterTheRead(FakeResourcePackRepository):
    """Rows whose next pack commits just after the GC has read them."""

    def __init__(self, late: ResourcePack) -> None:
        super().__init__()
        self._late = late

    async def list_all(self) -> list[ResourcePack]:
        rows = await super().list_all()
        await self.add(self._late)
        return rows


async def test_keeps_the_blob_of_a_row_committed_after_the_rows_were_read() -> None:
    # The blob is old and its row is missing from the rows the GC read, so neither
    # the listing nor the safety window spares it: only the re-read of the row
    # immediately before the delete sees the commit that landed in between.
    bucket = _Bucket()
    pack = await bucket.upload()
    bucket.rows = _RowCommitsAfterTheRead(late=pack)

    await bucket.gc()

    assert bucket.blob_keys() == [f"resource-packs/{pack.id.value}/{_FILENAME}"]


class _DatabaseDown(Exception):
    """The row read failing: the database, not the bucket."""


class _ReReadFails(FakeResourcePackRepository):
    """Rows the GC can list, whose re-read before a delete fails."""

    async def get_by_id(self, pack_id: ResourcePackId) -> ResourcePack | None:
        raise _DatabaseDown("re-read failed")


async def test_a_failed_re_read_ends_the_pass_and_is_not_a_failed_delete() -> None:
    # The re-read before each delete sits outside the per-pack ``try``: a re-read
    # that fails says nothing about the pack, and without it no pack may be
    # deleted. It leaves the pass rather than being counted as a failed delete
    # (issue #3906).
    bucket = _Bucket()
    keys = await bucket.orphans(2)
    bucket.rows = _ReReadFails()

    with pytest.raises(_DatabaseDown):
        await bucket.gc()

    assert sorted(bucket.blob_keys()) == keys


def _row(pack_id: ResourcePackId) -> ResourcePack:
    """The row of a pack stored under *pack_id*; the GC reads only its id."""

    return ResourcePack(
        id=pack_id,
        filename=_FILENAME,
        display_name="Pack",
        description=None,
        sha1_hash="",
        sha256_hash="",
        size_bytes=0,
        uploaded_by=uuid.uuid4(),
        created_at=_T0,
        updated_at=_T0,
    )


async def test_a_skipped_pack_leaves_the_count_of_failed_deletes_alone() -> None:
    # A real bucket mixes live and young packs among the orphans. A pack the pass
    # skips says nothing about the store, so it neither adds to the count nor
    # restarts it: five failures with a live, a young and a re-read-spared pack
    # between them still end the pass, and the pack after them is not tried
    # (issue #3906).
    bucket = _Bucket()
    ids = sorted(
        (ResourcePackId(uuid.uuid4()) for _ in range(9)), key=lambda p: str(p.value)
    )
    live, young, spared, last = ids[2], ids[4], ids[6], ids[8]

    async def _body() -> AsyncIterator[bytes]:
        yield _pack_zip()

    for pack_id in ids:
        # Stored two minutes after the rest, the young pack is still inside the
        # safety window when the pass meets it.
        bucket.clock.set(_T0 + dt.timedelta(minutes=2) if pack_id == young else _T0)
        await bucket.store.put(pack_id, _FILENAME, _body())
    bucket.clock.set(_T0)
    keys = [f"resource-packs/{pack_id.value}/{_FILENAME}" for pack_id in ids]
    failing = [
        key
        for pack_id, key in zip(ids, keys, strict=True)
        if pack_id not in (live, young, spared, last)
    ]
    bucket.faults.when = lambda op, key: op == "delete_object" and key in failing
    bucket.rows = _RowCommitsAfterTheRead(late=_row(spared))
    await bucket.rows.add(_row(live))

    result = await bucket.gc()

    assert sorted(bucket.blob_keys()) == keys
    assert (result.scanned, result.deleted, result.failed) == (9, 0, 5)


async def test_refuses_a_bucket_another_database_owns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A pack another database uploaded has no row here, so it looks orphaned:
    # the pass must refuse before it deletes the only copy (issue #3870).
    bucket = _Bucket()
    pack_id = ResourcePackId(uuid.uuid4())

    async def _body() -> AsyncIterator[bytes]:
        yield _pack_zip()

    await bucket.store.put(pack_id, _FILENAME, _body())
    ownership = FakeStoreOwnership.foreign()

    with caplog.at_level(logging.ERROR):
        await bucket.gc(ownership)

    assert bucket.blob_keys() == [f"resource-packs/{pack_id.value}/{_FILENAME}"]
    assert any(
        rec.levelno == logging.ERROR
        and "resource-pack GC refused" in rec.getMessage()
        and str(ownership.refusal_reason) in rec.getMessage()
        for rec in caplog.records
    )


async def test_a_pack_the_store_gives_no_answer_for_starves_no_other() -> None:
    # A delete that gets no answer ends the pass (issue #3871). When that failure
    # is in fact tied to one pack, the next pass starts after it and comes round to
    # it last, so every other orphan is reclaimed within two passes, and the pack
    # is still tried (issue #3934).
    bucket = _Bucket()
    keys = await bucket.orphans(5)
    bad = keys[1]
    bucket.faults.when = lambda op, key: op == "delete_object" and key == bad
    bucket.faults.error = ObjectStoreUnreachableError

    with pytest.raises(ResourcePackStorageUnreachableError):
        await bucket.gc()
    with pytest.raises(ResourcePackStorageUnreachableError):
        await bucket.gc()

    assert bucket.blob_keys() == [bad]


async def test_a_store_that_answers_no_delete_ends_each_pass_at_its_first() -> None:
    # A store that is truly down still ends each pass at once. Each pass starts
    # after the pack the last one ended at, so the passes take the packs in turn,
    # and the resume point is only ever that one pack's key (issue #3934).
    bucket = _Bucket()
    keys = await bucket.orphans(3)
    attempted = bucket.answer_no_delete()

    for _ in range(7):
        with pytest.raises(ResourcePackStorageUnreachableError):
            await bucket.gc()

    assert attempted == [*keys, *keys, keys[0]]
    assert bucket.resume_point.key == keys[0]
    assert sorted(bucket.blob_keys()) == keys


async def test_a_pass_that_comes_round_clears_the_resume_point() -> None:
    # A pass that walks the whole listing without a store failure leaves no resume
    # point, so the pass after it starts at the beginning (issue #3934).
    bucket = _Bucket()
    keys = await bucket.orphans(3)
    bucket.faults.when = lambda op, key: op == "delete_object" and key == keys[1]
    bucket.faults.error = ObjectStoreUnreachableError
    with pytest.raises(ResourcePackStorageUnreachableError):
        await bucket.gc()
    assert bucket.resume_point.key == keys[1]
    bucket.faults.clear()

    await bucket.gc()

    assert bucket.resume_point.key is None


async def test_a_resumed_pass_logs_where_it_starts_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Where a pass starts is worth a line only when debugging (issue #3934). The
    # key ends in the upload's filename, so it is logged escaped (issue #3907).
    bucket = _Bucket()
    (key,) = await bucket.orphans(1, filename=_FORGED_FILENAME)
    bucket.faults.when = lambda op, k: op == "delete_object"
    bucket.faults.error = ObjectStoreUnreachableError
    with pytest.raises(ResourcePackStorageUnreachableError):
        await bucket.gc()
    bucket.faults.clear()

    with caplog.at_level(logging.DEBUG, logger="mc_server_dashboard_api"):
        await bucket.gc()

    rendered = [
        TextFormatter().format(r) for r in caplog.records if r.levelno == logging.DEBUG
    ]
    (resumed,) = [line for line in rendered if repr(key) in line]
    assert "after" in resumed
    assert not _forged(resumed)


async def test_a_pass_five_failures_in_a_row_end_keeps_the_resume_point() -> None:
    # A pass that stops after five failed deletes in a row did not come round, so
    # the next pass still starts after the pack the store gave no answer for
    # (issue #3934).
    bucket = _Bucket()
    keys = await bucket.orphans(6)
    bucket.faults.when = lambda op, key: op == "delete_object" and key == keys[0]
    bucket.faults.error = ObjectStoreUnreachableError
    with pytest.raises(ResourcePackStorageUnreachableError):
        await bucket.gc()
    bucket.faults.clear()
    bucket.faults.when = lambda op, key: op == "delete_object" and key in keys[1:]

    result = await bucket.gc()

    assert result.failed == 5
    assert bucket.resume_point.key == keys[0]
