"""GET /metrics: Prometheus exposition endpoint (issue #282).

Refreshes the scrape-time gauges (servers by observed state, workers by state)
from the database and the in-memory worker registry, then renders the process-wide
metric registry in the Prometheus text exposition format. Unauthenticated but
safe-by-content (aggregates only). Operators should firewall it on an
internet-facing deployment (CONFIGURATION.md / SECURITY.md).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.core.adapters import metrics as metrics_module
from mc_server_dashboard_api.core.adapters.metrics_refresh import refresh_scrape_gauges
from mc_server_dashboard_api.dependencies import (
    get_metrics_session_factory,
    get_worker_registry,
)
from mc_server_dashboard_api.fleet.domain.registry import WorkerRegistry

router = APIRouter()


@router.get("/metrics")
async def metrics(
    session_factory: Annotated[
        async_sessionmaker[AsyncSession], Depends(get_metrics_session_factory)
    ],
    registry: Annotated[WorkerRegistry, Depends(get_worker_registry)],
) -> Response:
    await refresh_scrape_gauges(session_factory, registry)
    body, content_type = metrics_module.render()
    return Response(content=body, media_type=content_type)
