"""GET /workers: the platform-admin read surface for the Worker fleet.

A minimal operability endpoint (FR-WRK-2): list the registered Workers with
their advertised capabilities and current liveness. Guarded by the
platform-admin axis (FR-AUTHZ-5) since the fleet is cross-community
infrastructure, not community-scoped.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from mc_server_dashboard_api.dependencies import (
    get_list_workers,
    require_platform_admin,
)
from mc_server_dashboard_api.fleet.application.list_workers import ListWorkers
from mc_server_dashboard_api.fleet.domain.registry import WorkerSnapshot

router = APIRouter()


class HostResourcesResponse(BaseModel):
    cpu_cores: int
    memory_bytes: int


class CapabilitiesResponse(BaseModel):
    drivers: list[str]
    max_servers: int
    resources: HostResourcesResponse


class WorkerResponse(BaseModel):
    id: str
    version: str
    status: str
    registered_at: dt.datetime
    last_heartbeat_at: dt.datetime
    capabilities: CapabilitiesResponse


class WorkersResponse(BaseModel):
    workers: list[WorkerResponse]


def _to_response(snapshot: WorkerSnapshot) -> WorkerResponse:
    caps = snapshot.capabilities
    return WorkerResponse(
        id=snapshot.id.value,
        version=snapshot.version,
        status=snapshot.status.value,
        registered_at=snapshot.registered_at,
        last_heartbeat_at=snapshot.last_heartbeat_at,
        capabilities=CapabilitiesResponse(
            drivers=sorted(driver.value for driver in caps.drivers),
            max_servers=caps.max_servers,
            resources=HostResourcesResponse(
                cpu_cores=caps.resources.cpu_cores,
                memory_bytes=caps.resources.memory_bytes,
            ),
        ),
    )


@router.get("/workers", dependencies=[Depends(require_platform_admin)])
async def list_workers(
    use_case: Annotated[ListWorkers, Depends(get_list_workers)],
) -> WorkersResponse:
    return WorkersResponse(workers=[_to_response(w) for w in use_case()])
