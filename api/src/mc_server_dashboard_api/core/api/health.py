"""GET /healthz: liveness plus database-connectivity readiness.

The router is thin: it resolves the :class:`DatabasePing` Port via FastAPI
dependency injection (bound in the wiring layer), runs the :class:`CheckHealth`
use case, and serialises the report. A degraded database yields ``ok=false``
with HTTP 200 — the endpoint reports the state rather than failing.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from mc_server_dashboard_api.core.application.check_health import CheckHealth
from mc_server_dashboard_api.core.domain.health import DatabasePing
from mc_server_dashboard_api.dependencies import get_database_ping

router = APIRouter()


class HealthResponse(BaseModel):
    ok: bool
    database_reachable: bool


@router.get("/healthz")
async def healthz(
    database: Annotated[DatabasePing, Depends(get_database_ping)],
) -> HealthResponse:
    report = await CheckHealth(database=database)()
    return HealthResponse(ok=report.ok, database_reachable=report.database_reachable)
