"""GET /readyz: per-component readiness probe (issue #282).

Distinct from ``/healthz`` (the cheap liveness probe, unchanged): readiness is the
AND of the critical components — the database is reachable and, when enabled, the
control-plane gRPC server has started. The endpoint returns 200 with each
component's boolean when ready, and 503 with the same per-component shape when any
critical component is not ready, so an orchestrator gates traffic and an operator
sees which component failed. Unauthenticated but safe-by-content (component
booleans only).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from mc_server_dashboard_api.core.application.check_readiness import CheckReadiness
from mc_server_dashboard_api.core.domain.health import DatabasePing
from mc_server_dashboard_api.core.domain.readiness import ControlPlaneReadiness
from mc_server_dashboard_api.dependencies import (
    get_control_plane_readiness,
    get_database_ping,
)

router = APIRouter()


class ReadinessResponse(BaseModel):
    ready: bool
    components: dict[str, bool]


@router.get("/readyz")
async def readyz(
    response: Response,
    database: Annotated[DatabasePing, Depends(get_database_ping)],
    control_plane: Annotated[
        ControlPlaneReadiness, Depends(get_control_plane_readiness)
    ],
) -> ReadinessResponse:
    report = await CheckReadiness(database=database, control_plane=control_plane)()
    if not report.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        ready=report.ready,
        components={c.name: c.ready for c in report.components},
    )
