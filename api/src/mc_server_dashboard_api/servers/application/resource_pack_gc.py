"""Garbage collection of resource-pack blobs no row names (issue #3277).

The pack row is the source of truth: a blob no row names is never served, and
nothing else removes it. Two use cases can leave one behind, each ordering its
writes so a failure leaves an unserved blob rather than a row whose blob is gone.
``DeleteResourcePack`` commits the row's delete
before it removes the blob, so a store outage there leaves the blob (#1962).
``UploadResourcePack`` stores the blob before it commits the row, so a failed
commit -- or an upload the store completed but could not acknowledge -- leaves
the blob with no row (#2458). Mirrors the plugin-cache GC (#1332) in structure
and safety model.

**Reference model.** A stored blob is LIVE iff a ``resource_packs`` row carries
the pack id it is stored under. Every upload mints a fresh id, so a blob under an
id with no row is an orphan, whatever its filename.

**Safety window.** The window between an upload's ``put`` and its commit is
exactly an orphan to this GC. A blob younger than :data:`GC_SAFETY_WINDOW` is
therefore never deleted. The window has to cover the whole upload as well as the
commit, since a store may stamp a multipart object with the time the upload
began. The 256 MiB upload cap keeps that far below an hour.

**Re-check before delete.** The rows are read after the blobs are listed, so a
row committed before that read is seen. A row committed after it is caught by
re-reading that one row immediately before the delete (the #1404 pattern). That
narrows the race to the gap between the re-read and the delete without closing
it; losing it would take an upload whose commit lands more than the safety
window after its blob was stored.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.resource_pack_store import (
    ResourcePackStore,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork

# Never delete a blob younger than this. See module docstring.
GC_SAFETY_WINDOW = dt.timedelta(hours=1)


@dataclass(frozen=True)
class RunResourcePackGc:
    """Sweep the resource-pack blobs: delete unreferenced, old-enough packs."""

    uow: UnitOfWork
    store: ResourcePackStore
    clock: Clock

    async def __call__(self) -> None:
        entries = await self.store.list_entries()
        async with self.uow:
            live = {pack.id for pack in await self.uow.resource_packs.list_all()}
        cutoff = self.clock.now() - GC_SAFETY_WINDOW
        for entry in entries:
            if entry.pack_id in live:
                continue
            if entry.modified_at > cutoff:
                # Inside the safety window: may be an in-flight upload whose row
                # has not committed yet.
                continue
            # Re-read the row immediately before the delete: one committed since
            # the read above names this blob now.
            async with self.uow:
                if await self.uow.resource_packs.get_by_id(entry.pack_id) is not None:
                    continue
            await self.store.delete(entry.pack_id)
