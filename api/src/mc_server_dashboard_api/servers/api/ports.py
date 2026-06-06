"""HTTP edge for game-port availability reads (issue #243).

Two read-only endpoints over the deployment-wide game-port resource:

- ``GET /ports/check/{port}`` — whether a port is in the configured range and
  currently free.
- ``GET /ports/available?count=N`` — the next ``N`` free in-range ports.

**Auth choice.** A game port is a *deployment-wide* resource (unique across every
server), not scoped to a community — the same posture as the global version
catalog (see ``versions/api/versions.py``). There is no ``port:*`` permission to
scope to, and inventing one would be speculative, so these require only an
authenticated user (``get_current_user``): a read-only listing for any logged-in
user is proportionate.

The router is thin: resolve the read use cases via DI, run them, serialise.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from mc_server_dashboard_api.dependencies import (
    get_check_port,
    get_current_user,
    get_list_available_ports,
)
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.servers.application.port_availability import (
    CheckPort,
    ListAvailablePorts,
)

router = APIRouter(prefix="/ports")

# The most ports a single availability query may request. A sane upper bound that
# keeps the response small; a request above it is a 422 (FastAPI ``le``), not a
# silent clamp.
_MAX_COUNT = 100


class PortCheckResponse(BaseModel):
    """Availability of a single game port (issue #243)."""

    port: int
    in_range: bool
    available: bool


class AvailablePortsResponse(BaseModel):
    """The next free in-range game ports, ascending (issue #243)."""

    ports: list[int]


@router.get("/check/{port}")
async def check_port(
    port: int,
    _user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[CheckPort, Depends(get_check_port)],
) -> PortCheckResponse:
    """Report whether ``port`` is in range and currently free (issue #243)."""

    return PortCheckResponse.model_validate(await use_case(port=port))


@router.get("/available")
async def available_ports(
    _user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[ListAvailablePorts, Depends(get_list_available_ports)],
    count: Annotated[int, Query(ge=1, le=_MAX_COUNT)] = 1,
) -> AvailablePortsResponse:
    """Return the next ``count`` free in-range ports (issue #243)."""

    return AvailablePortsResponse(ports=await use_case(count=count))
