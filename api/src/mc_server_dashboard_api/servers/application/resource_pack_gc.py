"""Garbage collection of resource-pack blobs no row names (issue #3277).

The pack row is the source of truth: a blob no row names is never served, and
nothing else removes it. Two use cases can leave one behind, each ordering its
writes so a failure leaves an unserved blob rather than a row whose blob is gone.
``DeleteResourcePack`` commits the row's delete before it removes the blob, so a
store outage there leaves the blob (#1962). ``UploadResourcePack`` stores the
blob before it commits the row, so a failed commit -- or an upload the store
completed but could not acknowledge -- leaves the blob with no row (#2458).
Mirrors the plugin-cache GC (#1332) in structure and safety model.

**Reference model.** A stored blob is LIVE iff a ``resource_packs`` row carries
the pack id it is stored under. Every upload mints a fresh id, so a blob under an
id with no row is an orphan, whatever its filename.

**Safety window.** The window between an upload's ``put`` and its commit is
exactly an orphan to this GC. A blob younger than :data:`GC_SAFETY_WINDOW` is
therefore never deleted. The window has to cover the whole upload as well as the
commit, since a store may stamp a multipart object with the time the upload
began. That rests on throughput, not on a bound: a pack goes up as one part, and
below about 75 KB/s a 256 MiB pack takes longer than an hour. Such an upload is
left to the re-check below and its residual race.

**Re-check before delete.** The rows are read after the blobs are listed, so a
row committed before that read is seen. A row committed after it is caught by
re-reading that one row immediately before the delete (the #1404 pattern). That
narrows the race to the gap between the re-read and the delete without closing
it; losing it would take an upload whose commit lands more than the safety
window after its blob was stored.

**Ownership.** The reference model holds only if the bucket belongs to this
database: in a bucket another database also uses, that database's packs are named
by no row here, and each is the only copy of a user upload. Each pass therefore
asks the bucket's owner marker first and is refused, with an ERROR, unless it
names this installation (STORAGE.md Section 4.3, issue #3870).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.resource_pack_store import (
    ResourcePackStore,
)
from mc_server_dashboard_api.servers.domain.store_ownership import StoreOwnership
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork

_LOG = logging.getLogger(__name__)

# Never delete a blob younger than this. See module docstring.
GC_SAFETY_WINDOW = dt.timedelta(hours=1)


@dataclass(frozen=True)
class RunResourcePackGc:
    """Sweep the resource-pack blobs: delete unreferenced, old-enough packs."""

    uow: UnitOfWork
    store: ResourcePackStore
    clock: Clock
    ownership: StoreOwnership

    async def __call__(self) -> None:
        refusal = await self.ownership.refusal()
        if refusal is not None:
            _LOG.error("resource-pack GC refused: %s", refusal)
            return
        entries = await self.store.list_entries()
        async with self.uow:
            live = {pack.id for pack in await self.uow.resource_packs.list_all()}
        now = self.clock.now()
        cutoff = now - GC_SAFETY_WINDOW
        reclaimed = 0
        # The window is judged per blob, while ``store.delete`` removes every blob
        # under the pack id. The two agree because each upload puts one blob under
        # a freshly minted id; a second blob per pack would need the window judged
        # on the pack's newest blob instead.
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
            reclaimed += 1
            # The blob was the only copy of a user upload: say what went.
            _LOG.info(
                "resource-pack GC reclaimed pack %s (%s, stored %s ago): no row "
                "names it",
                entry.pack_id.value,
                entry.key,
                now - entry.modified_at,
            )
        _LOG.info(
            "resource-pack GC pass: %d blob(s) listed, %d reclaimed",
            len(entries),
            reclaimed,
        )
