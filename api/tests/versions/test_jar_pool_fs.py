"""Filesystem-specific JAR-pool behavior outside the shared Port contract."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.versions.adapters.storage_jar_pool import StorageJarPool

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
