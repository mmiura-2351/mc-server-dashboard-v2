"""The storage sweep runs only on a store this database owns (issue #3870).

The sweep deletes ``incoming/`` staging no lease in this process holds, superseded
snapshots, and, on the object backend, multipart uploads older than an hour. On a
store another database also uses, that is the other API's in-flight work. So both
adapters read the owner marker before each pass (STORAGE.md Section 4.3) and refuse
the pass, with an ERROR, unless it names this installation.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from pathlib import Path

import pytest

from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.adapters.ownership import (
    OWNER_MARKER,
    FsStoreOwnership,
    ObjectStoreOwnership,
)
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory
from tests.storage.helpers import new_scope

_OURS = uuid.UUID("00000000-0000-4000-8000-000000000001")
_THEIRS = uuid.UUID("00000000-0000-4000-8000-000000000002")
_LONG_AGO = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def _marker(installation_id: uuid.UUID) -> bytes:
    return json.dumps(
        {"installation_id": str(installation_id), "created_at": "2026-01-01T00:00:00Z"}
    ).encode()


def _refused(caplog: pytest.LogCaptureFixture) -> bool:
    return any(
        rec.levelno == logging.ERROR
        and "storage sweep" in rec.getMessage()
        and str(_THEIRS) in rec.getMessage()
        for rec in caplog.records
    )


async def _object_storage(owner: uuid.UUID) -> tuple[FakeS3Store, ObjectStorage, str]:
    store = FakeS3Store()
    store.objects[OWNER_MARKER] = _marker(owner)
    ownership = ObjectStoreOwnership(fake_s3_factory(store), bucket="mcsd")
    await ownership.claim(_OURS)
    community, server = new_scope()
    staged = (
        f"communities/{community.value}/servers/{server.value}/incoming/t1/level.dat"
    )
    store.objects[staged] = b"in flight"
    store.multipart_uploads["upload-old"] = (f"jars/{'0' * 64}.jar", _LONG_AGO)
    storage = ObjectStorage(fake_s3_factory(store), ownership=ownership)
    return store, storage, staged


async def test_object_sweep_refuses_a_store_another_database_owns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, storage, staged = await _object_storage(_THEIRS)

    with caplog.at_level(logging.ERROR):
        await storage.sweep()

    assert staged in store.objects
    assert "upload-old" in store.multipart_uploads
    assert _refused(caplog)


async def test_object_sweep_runs_on_a_store_this_database_owns() -> None:
    store, storage, staged = await _object_storage(_OURS)

    await storage.sweep()

    assert staged not in store.objects
    assert "upload-old" not in store.multipart_uploads


async def _fs_storage(tmp_path: Path, owner: uuid.UUID) -> tuple[FsStorage, Path]:
    (tmp_path / OWNER_MARKER).write_bytes(_marker(owner))
    ownership = FsStoreOwnership(tmp_path)
    await ownership.claim(_OURS)
    community, server = new_scope()
    staged = (
        tmp_path
        / "communities"
        / str(community.value)
        / "servers"
        / str(server.value)
        / "incoming"
        / "t1"
    )
    staged.mkdir(parents=True)
    (staged / "level.dat").write_bytes(b"in flight")
    return FsStorage(tmp_path, ownership=ownership), staged


async def test_fs_sweep_refuses_a_root_another_database_owns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    storage, staged = await _fs_storage(tmp_path, _THEIRS)

    with caplog.at_level(logging.ERROR):
        storage.sweep()

    assert staged.exists()
    assert _refused(caplog)


async def test_fs_sweep_runs_on_a_root_this_database_owns(tmp_path: Path) -> None:
    storage, staged = await _fs_storage(tmp_path, _OURS)

    storage.sweep()

    assert not staged.exists()
