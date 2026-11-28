"""Filesystem-specific JAR-pool behavior outside the shared Port contract."""

from __future__ import annotations

import datetime as dt
import errno
import os
from pathlib import Path

import pytest

from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.errors import (
    StorageUnavailableError,
    StorageUnreachableError,
)
from mc_server_dashboard_api.versions.adapters.storage_jar_pool import StorageJarPool
from mc_server_dashboard_api.versions.domain.errors import JarPoolUnreachableError

_JAR = b"jar-bytes"
_OLD_MTIME = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)


async def test_delete_then_reput_refreshes_filesystem_store_time(
    tmp_path: Path,
) -> None:
    pool = StorageJarPool(FsStorage(tmp_path))
    key = await pool.put(_JAR)
    jar_path = next((tmp_path / "jars").glob("*.jar"))
    os.utime(jar_path, (_OLD_MTIME.timestamp(), _OLD_MTIME.timestamp()))

    (old_entry,) = await pool.list_entries()
    assert old_entry.modified_at == _OLD_MTIME
    await pool.delete(key)
    await pool.put(_JAR)

    (entry,) = await pool.list_entries()
    assert entry.modified_at > old_entry.modified_at


def _fail_unlink(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    def _unlink(self: Path, missing_ok: bool = False) -> None:
        raise OSError(code, os.strerror(code), str(self))

    monkeypatch.setattr(Path, "unlink", _unlink)


@pytest.mark.parametrize(
    "code",
    [
        errno.EIO,
        errno.ENODEV,
        errno.ENXIO,
        errno.ESTALE,
        errno.ETIMEDOUT,
        errno.ENOTCONN,
        errno.EHOSTDOWN,
        errno.EHOSTUNREACH,
        errno.ENETDOWN,
        errno.ENETUNREACH,
    ],
)
async def test_a_delete_on_a_gone_store_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    # The device vanished, or the remote-fs mount went stale or lost its
    # transport: the store itself is gone, so the GC ends its pass (issue #3871).
    # EIO is one of them: a soft NFS mount reports a server that stopped answering
    # that way (issue #3906).
    pool = StorageJarPool(FsStorage(tmp_path))
    key = await pool.put(_JAR)
    _fail_unlink(monkeypatch, code)

    with pytest.raises(JarPoolUnreachableError) as excinfo:
        await pool.delete(key)

    cause = excinfo.value.__cause__
    assert isinstance(cause, StorageUnreachableError)
    assert isinstance(cause.__cause__, OSError)
    assert cause.__cause__.errno == code


@pytest.mark.parametrize("code", [errno.EBUSY, errno.EACCES, errno.EROFS])
async def test_a_delete_that_fails_at_once_is_not_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    # A file in use or a permission is about this file. A read-only filesystem is
    # about the whole store, but it fails at once rather than after a timeout.
    # The GC skips the JAR and goes on (issue #3871), and five failures in a row
    # end its pass (issue #3906). EBUSY is a transient fault, which every fs Port
    # operation reports as the wider StorageUnavailableError (issue #3316).
    pool = StorageJarPool(FsStorage(tmp_path))
    key = await pool.put(_JAR)
    _fail_unlink(monkeypatch, code)

    with pytest.raises((OSError, StorageUnavailableError)) as excinfo:
        await pool.delete(key)

    assert not isinstance(excinfo.value, StorageUnreachableError)
    fault: BaseException | None = excinfo.value
    if code == errno.EBUSY:
        assert isinstance(fault, StorageUnavailableError)
        fault = fault.__cause__
    assert isinstance(fault, OSError)
    assert fault.errno == code
