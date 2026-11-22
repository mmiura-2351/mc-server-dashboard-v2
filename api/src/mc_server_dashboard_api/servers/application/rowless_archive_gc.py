"""Reclaim backup archives that no backup row names (issue #4106).

A backup writes its archive before it records its row (issue #4086). A crash
between the two, a discard whose delete failed, or a COMMIT that raised and did
not apply leaves an archive with no row. No listing shows it and nothing else
removes it while the server lives, so this pass does. It runs from the storage
sweep loop, after the sweep, and once at startup (CONFIGURATION.md Section 5.15).
The archives of a deleted server are left alone: ``DeleteServer`` keeps one of
them on purpose, and telling it apart from a leftover is issue #4165.

**Reference model.** An archive under a live server is live iff one of the
server's backup rows names its ref. Every create and upload mints a fresh ref,
so an archive no row names is a leftover.

**Grace.** An archive younger than ``grace`` is kept. Both writers hold the
server's lifecycle lock from the archive write to the row commit, and the
deletes below take the same lock, so a writer that holds its lock is never
raced. The grace covers one that lost it (issue #3927) and goes on writing, and
a store that stamps a multipart object with the time the upload began. It must
therefore be longer than any backup takes.

**Lock and re-read.** The rows are read after the archives are listed. For a
server with a leftover past the grace, the pass takes the lifecycle lock, reads
the server and its rows again under it, and deletes only what the rows still do
not name, each delete after a check that the lock is still held. A server
deleted since the pass listed the servers is left alone: ``DeleteServer``
commits under the same lock. A server whose lock stays busy past its wait budget
is skipped until the next pass.

**Ownership.** A store another database also uses holds archives no row of this
database names. The pass therefore asks the owner marker first and is refused,
with an ERROR, unless it names this installation (STORAGE.md Section 4.3).

**Record and failures.** Each reclaimed archive is logged at INFO, as the other
GCs log what they reclaim. A store that fails to list a server's archives, or to
delete one, ends that server's pass with a WARNING, and the pass goes on with the
next server. So does a lifecycle lock lost before a delete: the deletes stop
there, and the next pass retries. A database read that fails, of the servers or
of a server's rows, ends the whole pass with a WARNING: the database is what
every later server would read too. The next pass retries. A database failure in
the lifecycle lock's own acquire is not caught: it propagates to the caller,
which logs it at ERROR.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.backup_store import BackupArchiveStore
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    LifecycleLockLostError,
    ServerBusyError,
)
from mc_server_dashboard_api.servers.domain.lifecycle_lock import LifecycleLock
from mc_server_dashboard_api.servers.domain.store_ownership import StoreOwnership
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReclaimRowlessBackupArchives:
    """Delete each live server's archives that no row names, past the grace."""

    uow: UnitOfWork
    backup_store: BackupArchiveStore
    clock: Clock
    ownership: StoreOwnership
    lifecycle_lock: LifecycleLock
    grace: dt.timedelta

    async def __call__(self) -> None:
        refusal = await self.ownership.refusal()
        if refusal is not None:
            _LOG.error("row-less backup archive reclaim refused: %s", refusal)
            return
        try:
            async with self.uow:
                servers = await self.uow.servers.list_all()
        except Exception as exc:  # noqa: BLE001 - logged; the next pass retries
            _log_rows_unreadable("the servers", exc)
            return
        for server in servers:
            if not await self._reclaim(server):
                return

    async def _reclaim(self, server: Server) -> bool:
        """Reclaim ``server``'s leftovers; return whether the pass goes on."""

        try:
            archives = await self.backup_store.list_archives(
                community_id=server.community_id, server_id=server.id
            )
        except Exception as exc:  # noqa: BLE001 - one server must not end the pass
            _LOG.warning(
                "row-less backup archive reclaim skipped server %s: its archives "
                "could not be listed: %s: %s",
                server.id.value,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return True
        try:
            async with self.uow:
                rows = await self.uow.backups.list_for_server(server.id)
        except Exception as exc:  # noqa: BLE001 - logged; the next pass retries
            _log_rows_unreadable(f"the backup rows of server {server.id.value}", exc)
            return False
        named = {row.storage_ref for row in rows}
        now = self.clock.now()
        leftovers = [
            archive
            for archive in archives
            if archive.storage_ref not in named
            and now - archive.modified_at >= self.grace
        ]
        if not leftovers:
            return True
        try:
            async with self.lifecycle_lock.hold(server.id) as lock:
                # A row committed since the read above names its archive now. A
                # server deleted since the pass listed the servers has no rows
                # left, and DeleteServer keeps one of its archives on purpose
                # (issue #4165). It commits under this lock, so a row read here
                # is a live server's.
                reading = f"the row of server {server.id.value}"
                try:
                    async with self.uow:
                        if await self.uow.servers.get_by_id(server.id) is None:
                            return True
                        reading = f"the backup rows of server {server.id.value}"
                        rows = await self.uow.backups.list_for_server(server.id)
                except Exception as exc:  # noqa: BLE001 - logged; next pass retries
                    _log_rows_unreadable(reading, exc)
                    return False
                named = {row.storage_ref for row in rows}
                for archive in leftovers:
                    if archive.storage_ref in named:
                        continue
                    await lock.ensure_held()
                    try:
                        await self.backup_store.delete(
                            community_id=server.community_id,
                            server_id=server.id,
                            storage_ref=archive.storage_ref,
                        )
                    except Exception as exc:  # noqa: BLE001 - as the listing
                        _LOG.warning(
                            "row-less backup archive reclaim could not delete "
                            "archive %s of server %s, and skipped the rest of the "
                            "server: %s: %s",
                            archive.storage_ref,
                            server.id.value,
                            type(exc).__name__,
                            exc,
                            exc_info=True,
                        )
                        return True
                    _LOG.info(
                        "row-less backup archive reclaim deleted archive %s of "
                        "server %s (stored %s ago): no backup row names it",
                        archive.storage_ref,
                        server.id.value,
                        now - archive.modified_at,
                    )
        except ServerBusyError:
            _LOG.info(
                "row-less backup archive reclaim skipped server %s: it is busy "
                "(another lifecycle operation holds it); the next pass retries",
                server.id.value,
            )
        except LifecycleLockLostError:
            _LOG.warning(
                "row-less backup archive reclaim skipped the rest of server %s: its "
                "lifecycle lock was lost; the next pass retries",
                server.id.value,
                exc_info=True,
            )
        return True


def _log_rows_unreadable(what: str, exc: Exception) -> None:
    _LOG.warning(
        "row-less backup archive reclaim ended the pass: the database could not "
        "read %s: %s: %s; the next pass retries",
        what,
        type(exc).__name__,
        exc,
        exc_info=True,
    )
