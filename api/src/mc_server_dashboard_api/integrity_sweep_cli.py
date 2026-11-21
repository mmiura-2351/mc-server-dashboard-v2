"""Admin command to fsck/quarantine existing backups & snapshots (issue #744).

The one-shot maintenance pass over artifacts that predate the create/restore
integrity gates (#749/#743): the 2026-06-09 destructive test left corrupt
snapshots/backups, and new-only gating never re-checks them. This command
explicitly runs the :class:`IntegritySweep` use case — it is *not* run on boot (an
extract-per-archive scan is heavy; the issue settled on an explicit command).

It wires the same adapters the app composes (config-selected ``Storage``, the
servers ``UnitOfWork``, the audit recorder) against the live database and store,
enumerates every server (or one with ``--server <uuid>``), and prints the summary.

Usage::

    cd api && uv run python -m mc_server_dashboard_api.integrity_sweep_cli
    cd api && uv run python -m mc_server_dashboard_api.integrity_sweep_cli \
        --server <server-uuid>

Configuration (database URL, storage backend) is read from the environment, the
same ``MCD_API_*`` settings the API server uses.

Exit status: 0 when the pass completed; 1 when the object store could not serve
an archive read; 2 on a bad argument; 3 when the database driver raised an error
mid-pass (SQLAlchemy ``DBAPIError``, such as a dropped connection or a terminated
backend) or a server's lifecycle lock was lost. A 1 or a 3 stops the pass with a
one-line message: verdicts already written this run are committed, and the sweep
is safe to re-run.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid

from sqlalchemy.exc import DBAPIError

from mc_server_dashboard_api.app import _build_storage, _resolve_config_file
from mc_server_dashboard_api.audit.adapters.clock import SystemClock as AuditSystemClock
from mc_server_dashboard_api.audit.adapters.recorder import LoggingAuditRecorder
from mc_server_dashboard_api.audit.adapters.writer import SqlAlchemyAuditWriter
from mc_server_dashboard_api.config import load_settings
from mc_server_dashboard_api.core.adapters.database import (
    create_engine,
    create_session_factory,
)
from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.lifecycle_lock import PgLifecycleLock
from mc_server_dashboard_api.servers.adapters.unit_of_work import SqlAlchemyUnitOfWork
from mc_server_dashboard_api.servers.application.integrity_sweep import (
    IntegritySweep,
    SweepSummary,
)
from mc_server_dashboard_api.servers.domain.errors import (
    BackupStorageUnavailableError,
    LifecycleLockLostError,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId

_LOG = logging.getLogger(__name__)


async def run(*, server_id: ServerId | None) -> SweepSummary:
    settings = load_settings(_resolve_config_file())
    engine = create_engine(
        settings.database.url,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
    )
    # The lifecycle lock pins its connection for the whole hold while the sweep's
    # unit of work and audit writer need one of their own, so the lock gets its
    # own engine rather than a slot in the configured pool, leaving that pool
    # whole for the sweep's work. One connection is enough: the sweep holds one
    # lock at a time.
    lock_engine = create_engine(settings.database.url, pool_size=1, max_overflow=0)
    try:
        session_factory = create_session_factory(engine)
        # The integrity sweep only reads the store, so it needs no owner marker
        # (issue #3870).
        storage, _ownership = _build_storage(settings)
        sweep = IntegritySweep(
            uow=SqlAlchemyUnitOfWork(session_factory),
            backup_store=StorageBackupStoreAdapter(storage=storage),
            audit=LoggingAuditRecorder(
                SqlAlchemyAuditWriter(session_factory, clock=AuditSystemClock())
            ),
            # The same per-server advisory lock the API's backup creation holds
            # (issue #3220): keyed on the server id in PostgreSQL, so it serializes
            # this process against the API's.
            lifecycle_lock=PgLifecycleLock(engine=lock_engine),
        )
        return await sweep(server_id=server_id)
    finally:
        await lock_engine.dispose()
        await engine.dispose()


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        prog="python -m mc_server_dashboard_api.integrity_sweep_cli",
        description="Fsck and quarantine existing backups & snapshots (issue #744).",
    )
    parser.add_argument(
        "--server",
        metavar="UUID",
        help="scope the sweep to a single server id (default: every server)",
    )
    args = parser.parse_args(argv)

    server_id: ServerId | None = None
    if args.server is not None:
        try:
            server_id = ServerId(uuid.UUID(args.server))
        except ValueError:
            sys.stderr.write(f"invalid --server uuid: {args.server!r}\n")
            return 2

    try:
        summary = asyncio.run(run(server_id=server_id))
    except BackupStorageUnavailableError as exc:
        # The store could not serve an archive read (issue #2371). The pass stops
        # rather than misclassify — an outage is no verdict about a backup — so this
        # is an expected operator-facing outcome, not a crash. The aborting backup is
        # named in the preceding ERROR log line; the ref is repeated here so the
        # message stands alone.
        sys.stderr.write(
            f"integrity sweep aborted: the object store could not serve the archive "
            f"read for backup ref {exc}. No health verdict was written for it, and "
            f"verdicts already written this run are committed. Re-run the sweep once "
            f"the store is healthy.\n"
        )
        return 1
    except LifecycleLockLostError as exc:
        # The server's lifecycle lock was lost mid-pass (issue #3927), so the pass
        # stopped before the verdict write the lock guarded. Like a store outage
        # this is an operator-facing outcome, not a crash (issue #3942): one line,
        # with the traceback at DEBUG only.
        _LOG.debug("integrity sweep: the lifecycle lock was lost", exc_info=True)
        sys.stderr.write(
            f"integrity sweep aborted: the lifecycle lock on server {exc} was lost, "
            f"so the sweep stopped. Backups already classified this run keep their "
            f"verdict, and it is safe to re-run the sweep.\n"
        )
        return 3
    except DBAPIError as exc:
        # The database failed mid-pass: a dropped connection, a terminated backend
        # (issue #3942). The asyncpg dialect raises an error it has no narrower
        # class for, such as a backend terminated in flight, as the bare
        # DBAPIError, and never raises OperationalError, so DBAPIError is the
        # class caught; any other error is a bug and keeps its traceback.
        # ``exc.orig`` is the driver's one-line reason; the SQLAlchemy message
        # adds the statement and a URL over several lines.
        _LOG.debug("integrity sweep: the database failed", exc_info=True)
        sys.stderr.write(
            f"integrity sweep aborted: the database failed "
            f"({type(exc).__name__}: {exc.orig}), so the sweep stopped. Backups "
            f"already classified this run keep their verdict, and it is safe to "
            f"re-run the sweep.\n"
        )
        return 3
    print(
        f"servers scanned: {summary.servers_scanned}\n"
        f"backups healthy: {summary.backups_healthy}\n"
        f"backups quarantined (corrupt world): {summary.backups_quarantined}\n"
        f"backups unreadable (archive bytes unproducible): "
        f"{summary.backups_unreadable}\n"
        f"backups dangling: {summary.backups_dangling}\n"
        f"backups skipped (server busy; re-run the sweep to check them): "
        f"{summary.backups_skipped_busy}\n"
        f"snapshots scanned: {summary.snapshots_scanned}\n"
        f"snapshots flagged: {summary.snapshots_flagged}\n"
        f"snapshots not examined (backend does not fsck snapshots at rest): "
        f"{summary.snapshots_not_examined}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
