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

Configuration (database URL, storage backend, log level and format) is read from
the environment, the same ``MCD_API_*`` settings the API server uses, and the
command logs through the API's handler (issue #3942).

Exit status: 0 when the pass completed; 1 when the object store could not serve
an archive read; 2 on a bad argument or configuration, with one line; 3 when the
database failed or a server's lifecycle lock was lost. The database fails when it
refuses a connection, at the start or mid-pass, when it does not answer within
``database.connect_timeout_seconds`` or ``database.statement_timeout_seconds``,
or when the driver raises an error (SQLAlchemy ``DBAPIError``, such as a dropped
connection or a terminated backend). A 1 or a 3 stops the pass with a one-line
message: verdicts already written this run are committed, and the sweep is safe
to re-run.
``log.level=debug`` logs the traceback of a 3. Any other error, a full disk on
the store for one, ends in a traceback, and Python exits 1 for it too, so a 1
alone does not prove a store outage.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from mc_server_dashboard_api.app import _build_storage, _resolve_config_file
from mc_server_dashboard_api.audit.adapters.clock import SystemClock as AuditSystemClock
from mc_server_dashboard_api.audit.adapters.recorder import LoggingAuditRecorder
from mc_server_dashboard_api.audit.adapters.writer import SqlAlchemyAuditWriter
from mc_server_dashboard_api.config import (
    DatabaseSettings,
    Settings,
    describe_settings_error,
    load_settings,
)
from mc_server_dashboard_api.core.adapters.database import (
    DatabaseConnectError,
    create_engine,
    create_session_factory,
    is_transient_database_error,
    translate_connect_outages,
)
from mc_server_dashboard_api.logging import configure_logging
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


def _engine(
    database: DatabaseSettings, *, pool_size: int, max_overflow: int
) -> AsyncEngine:
    """An engine whose connect outage is a :class:`DatabaseConnectError`.

    asyncpg raises a refused connect as the raw ``OSError`` a store read can also
    raise, so ``main`` could not tell a database outage from a disk fault by type
    (issue #3942). Both of the sweep's engines are built here, held to the
    configured connect and statement bounds (issue #4042) and pool wait (issue
    #4051).
    """

    engine = create_engine(
        database.url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=database.pool_timeout_seconds,
        connect_timeout=database.connect_timeout_seconds,
        statement_timeout=database.statement_timeout_seconds,
    )
    translate_connect_outages(engine)
    return engine


def _is_database_failure(exc: Exception) -> bool:
    """Whether ``exc`` means the database failed, which ends the sweep with 3.

    A bare ``OSError`` never does: the fs adapter keeps a full disk or a permission
    fault raw (STORAGE.md Section 7.3), and the database's own connect failure
    and timeout arrive typed (``_engine``). The rest of what the app counts as a
    database outage, and every ``DBAPIError``, can only be the database's.
    """

    if isinstance(exc, OSError):
        return False
    return isinstance(
        exc, DBAPIError | DatabaseConnectError
    ) or is_transient_database_error(exc)


async def run(settings: Settings, *, server_id: ServerId | None) -> SweepSummary:
    engine = _engine(
        settings.database,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
    )
    # The lifecycle lock pins its connection for the whole hold while the sweep's
    # unit of work and audit writer need one of their own, so the lock gets its
    # own engine rather than a slot in the configured pool, leaving that pool
    # whole for the sweep's work. One connection is enough: the sweep holds one
    # lock at a time.
    lock_engine = _engine(settings.database, pool_size=1, max_overflow=0)
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
        settings = load_settings(_resolve_config_file())
    except (ValueError, OSError) as exc:
        # Settings that do not validate, or a config file that cannot be read: one
        # line and the bad-argument status, not a traceback (issue #3942).
        sys.stderr.write(
            f"integrity sweep: invalid configuration: {describe_settings_error(exc)}\n"
        )
        return 2
    # The API's handler, level and format (issue #3942), so the sweep's lines are
    # escaped as the API's are (issue #3938).
    configure_logging(settings.log.level, settings.log.format)
    try:
        summary = asyncio.run(run(settings, server_id=server_id))
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
    except Exception as exc:
        # The database failed: it refused a connection, at the start or mid-pass,
        # or dropped one, or a backend was terminated (issue #3942). The asyncpg
        # dialect raises an error it has no narrower class for, such as a backend
        # terminated in flight, as the bare DBAPIError, and never raises
        # OperationalError, so every DBAPIError counts. Any other error is a bug or
        # a standing fault and keeps its traceback.
        if not _is_database_failure(exc):
            raise
        _LOG.debug("integrity sweep: the database failed", exc_info=True)
        # ``exc.orig`` is the driver's one-line reason; a DBAPIError's own message
        # adds the statement and a URL over several lines.
        detail = exc.orig if isinstance(exc, DBAPIError) else exc
        sys.stderr.write(
            f"integrity sweep aborted: the database failed "
            f"({type(exc).__name__}: {detail}), so the sweep stopped. Backups "
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
