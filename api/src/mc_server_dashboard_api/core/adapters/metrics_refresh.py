"""Scrape-time refresh of the servers / workers gauges (issue #282).

The ``/metrics`` route calls :func:`refresh_scrape_gauges` on each scrape so the
``servers`` and ``workers`` gauges reflect the current fleet. Kept out of the
metrics-primitives module so that module stays a pure declaration of the metric
objects; this is the adapter that fills them from the database and the in-memory
worker registry.

Servers are counted by a single bounded ``GROUP BY observed_state`` query through
a short-lived session opened from the app's session factory — there is no cheaper
existing collection seam, and a per-scrape count is the smallest honest source.
If the database is unreachable the query failure is swallowed: the ``servers``
gauge is left untouched and ``servers_by_state_scrape_failures_total`` is bumped,
so /metrics never fails because the DB is down (issue #282). The workers gauge is
filled from the in-memory registry, which never touches the database.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.core.adapters import metrics
from mc_server_dashboard_api.fleet.domain.entities import WorkerStatus
from mc_server_dashboard_api.fleet.domain.registry import WorkerRegistry
from mc_server_dashboard_api.servers.adapters.models import ServerModel

_LOG = logging.getLogger(__name__)


async def refresh_scrape_gauges(
    session_factory: async_sessionmaker[AsyncSession],
    registry: WorkerRegistry,
) -> None:
    """Refresh the servers + workers gauges from the DB and registry."""

    await _refresh_servers(session_factory)
    _refresh_workers(registry)


async def _refresh_servers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    try:
        async with session_factory() as session:
            rows = await session.execute(
                select(ServerModel.observed_state, func.count()).group_by(
                    ServerModel.observed_state
                )
            )
            counts = {state: count for state, count in rows.all()}
    except Exception:
        # Never break /metrics when the DB is down: leave the gauge as-is and
        # record the scrape failure (issue #282).
        metrics.servers_by_state_scrape_failures_total.inc()
        _LOG.warning("servers-by-state scrape query failed", exc_info=True)
        return
    for state in _OBSERVED_STATES:
        metrics.servers.labels(observed_state=state).set(counts.get(state, 0))


def _refresh_workers(registry: WorkerRegistry) -> None:
    counts = {status: 0 for status in WorkerStatus}
    for snapshot in registry.list_workers():
        counts[snapshot.status] += 1
    for status, count in counts.items():
        metrics.workers.labels(state=status.value).set(count)


# The observed_state values always emitted so a state with zero servers reports 0
# rather than vanishing from the series (mirrors the server table's CHECK
# constraint, DATABASE.md Section 7).
_OBSERVED_STATES = (
    "starting",
    "running",
    "stopping",
    "stopped",
    "restarting",
    "crashed",
    "unknown",
)
