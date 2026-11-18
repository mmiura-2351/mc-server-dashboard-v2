"""The upload doors under load: the admission and the body read (issue #3843).

Both upload doors take archives of up to 512 MiB: a backup upload, and a files
upload with ``extract``. They validate on one small pool of their own, so they
admit only a few archive uploads at a time between them and refuse the rest at
once. Copying such a body on the event loop stalls every other request on the
process, so the route reads the spooled part in one call that runs on another
thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import threading
from types import ModuleType

import pytest
from fastapi import FastAPI, Request, UploadFile

from mc_server_dashboard_api.dependencies import get_archive_upload_admission
from mc_server_dashboard_api.servers.api import backups as backups_api
from mc_server_dashboard_api.servers.api import files as files_api
from mc_server_dashboard_api.servers.application.archive_validation import (
    ArchiveUploadAdmission,
)
from mc_server_dashboard_api.servers.domain.errors import UploadBusyError

_DOORS = [
    pytest.param(backups_api, id="backup-upload"),
    pytest.param(files_api, id="files-upload"),
]


# --- the admission -----------------------------------------------------------


def _assert_free(admission: ArchiveUploadAdmission) -> None:
    with admission.slot():
        pass


def test_the_doors_admit_two_validating_and_two_waiting_then_refuse() -> None:
    admission = ArchiveUploadAdmission()

    with contextlib.ExitStack() as held:
        for _ in range(4):
            held.enter_context(admission.slot())
        with pytest.raises(UploadBusyError), admission.slot():
            pass

    _assert_free(admission)


def test_a_slot_frees_when_its_upload_ends() -> None:
    admission = ArchiveUploadAdmission(limit=1)

    with admission.slot():
        pass

    _assert_free(admission)


def test_a_slot_frees_when_its_upload_fails() -> None:
    admission = ArchiveUploadAdmission(limit=1)

    with pytest.raises(ValueError), admission.slot():
        raise ValueError("invalid archive")

    _assert_free(admission)


async def test_a_slot_frees_when_its_upload_is_cancelled() -> None:
    admission = ArchiveUploadAdmission(limit=1)
    holding = asyncio.Event()

    async def upload() -> None:
        with admission.slot():
            holding.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(upload())
    await holding.wait()
    with pytest.raises(UploadBusyError), admission.slot():
        pass
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    _assert_free(admission)


def test_both_doors_count_against_one_admission() -> None:
    # The backup upload and the files extract validate on the same pool, so the
    # limit counts their uploads together.
    app = FastAPI()

    def request() -> Request:
        return Request({"type": "http", "app": app})

    assert get_archive_upload_admission(request()) is get_archive_upload_admission(
        request()
    )


# --- the body read ----------------------------------------------------------


class _ThreadRecordingPart(io.BytesIO):
    """A multipart part spooled to disk, recording each read's thread and bytes.

    Starlette reads a part on its thread pool unless the part is still in memory,
    which it tells by the spool's ``_rolled`` flag. This part has none, so it
    reads as a part spooled to disk, as every part past 1 MiB is.
    """

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.reads: list[tuple[int, bytes]] = []

    def read(self, size: int | None = -1) -> bytes:
        out = super().read(size)
        self.reads.append((threading.get_ident(), out))
        return out


@pytest.mark.parametrize("door", _DOORS)
async def test_the_upload_body_is_read_off_the_event_loop(door: ModuleType) -> None:
    # Larger than the 1 MiB reads the route used to join on the loop.
    body = bytes(range(256)) * (3 * 4096 + 1)
    part = _ThreadRecordingPart(body)

    content = await door._read_capped_upload(UploadFile(part))

    assert content == body
    loop_thread = threading.get_ident()
    assert part.reads
    assert all(thread != loop_thread for thread, _ in part.reads)
    # The bytes handed on are the very bytes a read made on that other thread,
    # so the loop copied nothing.
    assert any(out is content for _, out in part.reads)
