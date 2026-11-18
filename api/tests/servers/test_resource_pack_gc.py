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

    def blob_keys(self) -> list[str]:
        return [
            key for key in self.backing.objects if key.startswith("resource-packs/")
        ]

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

    async def orphans(self, count: int) -> list[str]:
        """Store *count* blobs no row names; their keys, in the order a pass meets
        them."""

        async def _body() -> AsyncIterator[bytes]:
            yield _pack_zip()

        for _ in range(count):
            await self.store.put(ResourcePackId(uuid.uuid4()), _FILENAME, _body())
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
    assert all(key.split("/")[1] not in warning.getMessage() for key in others)
    summary = caplog.records[-1]
    assert summary.levelno == logging.INFO
    assert "1 failed" in summary.getMessage()


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
