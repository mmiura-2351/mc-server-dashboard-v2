"""The /workers platform-admin surface for the Worker fleet.

A minimal operability surface guarded by the platform-admin axis (FR-AUTHZ-5)
since the fleet is cross-community infrastructure, not community-scoped:

- ``GET /workers`` lists the registered Workers with their advertised
  capabilities, current liveness, and load (FR-WRK-2).
- ``PUT``/``DELETE /workers/{worker_id}/drain`` set or clear a Worker's drain
  flag (FR-WRK-5, worker:manage); a draining Worker is excluded from placement.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from mc_server_dashboard_api.dependencies import (
    get_list_workers,
    get_set_worker_drain,
    require_platform_admin,
)
from mc_server_dashboard_api.fleet.application.list_workers import ListWorkers
from mc_server_dashboard_api.fleet.application.set_worker_drain import SetWorkerDrain
from mc_server_dashboard_api.fleet.domain.registry import WorkerSnapshot
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId

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
    assigned_count: int
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
        assigned_count=snapshot.assigned_count,
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


@router.put(
    "/workers/{worker_id}/drain",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_platform_admin)],
)
async def set_worker_drain(
    worker_id: str,
    use_case: Annotated[SetWorkerDrain, Depends(get_set_worker_drain)],
) -> None:
    if not use_case(worker_id=WorkerId(worker_id), draining=True):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")


@router.delete(
    "/workers/{worker_id}/drain",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_platform_admin)],
)
async def clear_worker_drain(
    worker_id: str,
    use_case: Annotated[SetWorkerDrain, Depends(get_set_worker_drain)],
) -> None:
    if not use_case(worker_id=WorkerId(worker_id), draining=False):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
