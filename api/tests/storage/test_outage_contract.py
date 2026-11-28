"""A storage I/O fault is ``StorageUnavailableError`` on every Port method (#3316).

The outage contract both backends give: whichever Port method a fault of the
store's own I/O strikes, the caller sees the one retryable storage type, never a
raw ``OSError``. The servers seams map it to ``503 storage_unavailable`` on the
routes a retry is safe for.

``_CALLS`` holds one call for every abstract method of the ``Storage`` Port and
of the ``WorkingSetView`` it hands out, and the first test pins that it covers
both, so a Port method added without an entry fails here. Each call runs against
one published server that holds every kind of stored thing, with the fault on
from before the call:

- ``eio``: every job the adapter runs off the event loop raises ``OSError(EIO)``.
  On fs that is all of its filesystem I/O. On object it is only the local disk
  work, the spools a few operations stage through, so only those meet it.
- ``store_outage`` (object): every S3 call raises what the object client raises
  for a store that answered with a 5xx.

A method a fault cannot reach is listed in ``_UNREACHABLE`` with the reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.errors import StorageUnavailableError
from mc_server_dashboard_api.storage.domain.port import (
    SnapshotHandle,
    Storage,
    WorkingSetView,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    BackupKey,
    CommunityId,
    JarKey,
    RelPath,
    ServerId,
    VersionId,
)
from tests.storage.fake_s3 import FakeS3Store
from tests.storage.faulty_s3 import Faults, faulty_s3_factory
from tests.storage.helpers import (
    drain,
    healthy_region_bytes,
    new_scope,
    region_targz,
    stream_of,
    tar_stream,
)

_FILES = {
    "server.properties": b"motd=old\n",
    "world/level.dat": b"level",
    "world/region/r.0.0.mca": healthy_region_bytes(),
}
_FILE = RelPath("server.properties")
_DIR = RelPath("world")


class _Rig:
    """One published server holding every kind of stored thing, and its fault."""

    def __init__(self, backend: str, tmp_path: Path) -> None:
        self.backend = backend
        self.scope: tuple[CommunityId, ServerId] = new_scope()
        self._faults = Faults()
        self.storage: Storage = (
            FsStorage(tmp_path)
            if backend == "fs"
            else ObjectStorage(faulty_s3_factory(FakeS3Store(), self._faults))
        )
        self._views = contextlib.AsyncExitStack()
        self._executor: tuple[asyncio.AbstractEventLoop, Any] | None = None
        self.backup: BackupKey
        self.jar: JarKey
        self.version: VersionId
        self.begun: SnapshotHandle
        self.staged: SnapshotHandle
        self.view: WorkingSetView

    async def arrange(self) -> None:
        storage = self.storage
        handle = await storage.begin_snapshot(*self.scope)
        await storage.write_snapshot(handle, tar_stream(_FILES))
        await storage.commit_snapshot(handle)
        # Rewriting the file retains its first content as a version.
        await storage.write_file(*self.scope, _FILE, b"motd=new\n")
        (self.version,) = await storage.list_file_versions(*self.scope, _FILE)
        self.backup = await storage.create_backup_from_current(*self.scope)
        self.jar = await storage.put_jar(stream_of(b"jar-bytes"))
        self.begun = await storage.begin_snapshot(*self.scope)
        self.staged = await storage.begin_snapshot(*self.scope)
        await storage.write_snapshot(self.staged, tar_stream(_FILES))
        self.view = await self._views.enter_async_context(
            storage.open_working_set_view(*self.scope)
        )

    def fail(self, fault: str) -> None:
        """From now on, the store's I/O fails with ``fault``."""

        if fault == "store_outage":
            self._faults.always()
            return
        loop = asyncio.get_running_loop()
        submit = loop.run_in_executor

        def _failing(executor: Any, fn: Callable[..., Any], *args: Any) -> Any:
            def _eio() -> None:
                raise OSError(errno.EIO, os.strerror(errno.EIO))

            return submit(executor, _eio)

        self._executor = (loop, submit)
        loop.run_in_executor = _failing  # type: ignore[method-assign,assignment]

    async def close(self) -> None:
        if self._executor is not None:
            loop, submit = self._executor
            loop.run_in_executor = submit  # type: ignore[method-assign]
        self._faults.clear()
        await self._views.aclose()


async def _enter_view(r: _Rig) -> None:
    async with r.storage.open_working_set_view(*r.scope):
        pass


_Call = Callable[[_Rig], Awaitable[object]]

# One call per Port method; a ``WorkingSetView`` method is ``view.<name>``, made
# on the view the rig opened before the fault.
_CALLS: dict[str, _Call] = {
    # --- working-set hydrate / snapshot (STORAGE.md Section 3.1)
    "open_hydrate_source": lambda r: drain(r.storage.open_hydrate_source(*r.scope)),
    "begin_snapshot": lambda r: r.storage.begin_snapshot(*r.scope),
    "write_snapshot": lambda r: r.storage.write_snapshot(r.begun, tar_stream(_FILES)),
    "commit_snapshot": lambda r: r.storage.commit_snapshot(r.staged),
    "abort_snapshot": lambda r: r.storage.abort_snapshot(r.begun),
    "current_generation": lambda r: r.storage.current_generation(*r.scope),
    "settled_generation": lambda r: r.storage.settled_generation(*r.scope),
    "current_publisher": lambda r: r.storage.current_publisher(*r.scope),
    "check_current_health": lambda r: r.storage.check_current_health(*r.scope),
    "prune_to_final_snapshot": lambda r: r.storage.prune_to_final_snapshot(*r.scope),
    # --- JAR store (Section 3.2)
    "put_jar": lambda r: r.storage.put_jar(stream_of(b"other-jar-bytes")),
    "has_jar": lambda r: r.storage.has_jar(r.jar),
    "open_jar": lambda r: drain(r.storage.open_jar(r.jar)),
    "jar_pool_stats": lambda r: r.storage.jar_pool_stats(),
    "list_jars": lambda r: r.storage.list_jars(),
    "delete_jar": lambda r: r.storage.delete_jar(r.jar),
    # --- backups (Section 3.3)
    "create_backup_from_current": lambda r: r.storage.create_backup_from_current(
        *r.scope
    ),
    "list_backups": lambda r: r.storage.list_backups(*r.scope),
    "list_backup_entries": lambda r: r.storage.list_backup_entries(*r.scope),
    "restore_backup": lambda r: r.storage.restore_backup(*r.scope, r.backup),
    "check_backup_health": lambda r: r.storage.check_backup_health(*r.scope, r.backup),
    "delete_backup": lambda r: r.storage.delete_backup(*r.scope, r.backup),
    "open_backup": lambda r: drain(r.storage.open_backup(*r.scope, r.backup)),
    "put_backup": lambda r: r.storage.put_backup(
        *r.scope, stream_of(region_targz(_FILES))
    ),
    "backup_size": lambda r: r.storage.backup_size(*r.scope, r.backup),
    # --- files (Section 3.4)
    "read_file": lambda r: r.storage.read_file(*r.scope, _FILE),
    "open_file_stream": lambda r: drain(r.storage.open_file_stream(*r.scope, _FILE)),
    "list_dir": lambda r: r.storage.list_dir(*r.scope, _DIR),
    "path_exists": lambda r: r.storage.path_exists(*r.scope, _DIR),
    "write_file": lambda r: r.storage.write_file(*r.scope, _FILE, b"motd=x\n"),
    "delete_file": lambda r: r.storage.delete_file(*r.scope, _FILE),
    "delete_dir": lambda r: r.storage.delete_dir(*r.scope, _DIR),
    "rename_file": lambda r: r.storage.rename_file(
        *r.scope, _FILE, RelPath("renamed.properties")
    ),
    "rename_dir": lambda r: r.storage.rename_dir(*r.scope, _DIR, RelPath("world2")),
    "subtree_extent": lambda r: r.storage.subtree_extent(
        *r.scope, RelPath("."), max_depth=8
    ),
    "make_dir": lambda r: r.storage.make_dir(*r.scope, RelPath("plugins")),
    "open_working_set_view": _enter_view,
    # --- file versions (Section 3.5)
    "list_file_versions": lambda r: r.storage.list_file_versions(*r.scope, _FILE),
    "retain_file_version": lambda r: r.storage.retain_file_version(*r.scope, _FILE),
    "read_file_version": lambda r: r.storage.read_file_version(
        *r.scope, _FILE, r.version
    ),
    "rollback_file": lambda r: r.storage.rollback_file(*r.scope, _FILE, r.version),
    # --- the pinned view (Section 3.4)
    "view.__aenter__": _enter_view,
    "view.__aexit__": lambda r: r.view.__aexit__(None, None, None),
    "view.list_dir": lambda r: r.view.list_dir(_DIR),
    "view.open_file_stream": lambda r: drain(r.view.open_file_stream(_FILE)),
}

_PORT_METHODS = Storage.__abstractmethods__ | {
    f"view.{name}" for name in WorkingSetView.__abstractmethods__
}

_RELEASES_A_LEASE = "releases the view's lease, which is held in memory"

# The object backend's only local disk I/O: the spools these operations stage the
# bytes they move through.
_OBJECT_SPOOLING = {
    "write_snapshot",
    "prune_to_final_snapshot",
    "put_jar",
    "create_backup_from_current",
    "restore_backup",
    "check_backup_health",
}

# Per backend and fault, the methods the fault cannot reach, and why.
_UNREACHABLE: dict[tuple[str, str], dict[str, str]] = {
    ("fs", "eio"): {"view.__aexit__": _RELEASES_A_LEASE},
    ("object", "eio"): {
        name: "does no local disk I/O: its store is S3 (the store_outage cases)"
        for name in _PORT_METHODS - _OBJECT_SPOOLING
    },
    ("object", "store_outage"): {
        "begin_snapshot": "only registers the staging prefix; objects come later",
        "check_current_health": "examines nothing on this backend, and says so",
        "view.__aexit__": _RELEASES_A_LEASE,
    },
}


def test_every_port_method_has_a_call() -> None:
    assert set(_CALLS) == _PORT_METHODS
    for unreachable in _UNREACHABLE.values():
        assert set(unreachable) <= _PORT_METHODS


# 19 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(19 * 10 + 30)
@pytest.mark.parametrize(
    ("backend", "fault", "method"),
    [
        pytest.param(backend, fault, method, id=f"{backend}-{fault}-{method}")
        for (backend, fault), unreachable in _UNREACHABLE.items()
        for method in sorted(_PORT_METHODS - set(unreachable))
    ],
)
async def test_a_storage_io_fault_is_storage_unavailable(
    tmp_path: Path, backend: str, fault: str, method: str
) -> None:
    rig = _Rig(backend, tmp_path)
    await rig.arrange()
    rig.fail(fault)
    try:
        with pytest.raises(StorageUnavailableError):
            await _CALLS[method](rig)
    finally:
        await rig.close()
