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
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid

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
from mc_server_dashboard_api.servers.adapters.unit_of_work import SqlAlchemyUnitOfWork
from mc_server_dashboard_api.servers.application.integrity_sweep import (
    IntegritySweep,
    SweepSummary,
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
    try:
        session_factory = create_session_factory(engine)
        storage = _build_storage(settings)
        sweep = IntegritySweep(
            uow=SqlAlchemyUnitOfWork(session_factory),
            backup_store=StorageBackupStoreAdapter(storage=storage),
            audit=LoggingAuditRecorder(
                SqlAlchemyAuditWriter(session_factory, clock=AuditSystemClock())
            ),
        )
        return await sweep(server_id=server_id)
    finally:
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

    summary = asyncio.run(run(server_id=server_id))
    print(
        f"servers scanned: {summary.servers_scanned}\n"
        f"backups healthy: {summary.backups_healthy}\n"
        f"backups quarantined: {summary.backups_quarantined}\n"
        f"snapshots scanned: {summary.snapshots_scanned}\n"
        f"snapshots flagged: {summary.snapshots_flagged}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
