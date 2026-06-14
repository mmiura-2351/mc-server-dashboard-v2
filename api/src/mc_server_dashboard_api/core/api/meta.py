"""GET /meta: deployment facts the Web UI needs before any server exists.

Currently a single flag — whether the game-ingress relay is enabled. The create
form uses it to decide whether to surface the game-port control: in relay mode
the port is internal plumbing the API auto-allocates and players join port-less
via ``<slug>.<base_domain>`` (issue #1002, RELAY.md Section 13), so the control
is hidden. Per-server responses already carry ``join_hostname`` for the same
signal once a server exists; this endpoint covers the create flow, which has no
server yet.

The router is thin: read the resolved settings and serialise. It requires an
authenticated user (the same posture as ``/ports``): a logged-in user reading a
deployment fact is proportionate, and there is no narrower permission to scope to.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from mc_server_dashboard_api.config import Settings
from mc_server_dashboard_api.dependencies import get_current_user, get_settings
from mc_server_dashboard_api.identity.domain.entities import User

router = APIRouter()


class MetaResponse(BaseModel):
    """Deployment facts the Web UI reads before a server exists (issue #1002)."""

    relay_enabled: bool
    # Operator-configurable memory-limit knobs (issue #1069). ``None`` means the
    # operator has not set them and the hardcoded defaults apply.
    default_memory_limit_mb: int | None
    max_memory_limit_mb: int | None


@router.get("/meta")
async def meta(
    _user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MetaResponse:
    """Report deployment-wide UI facts (currently: whether the relay is on)."""

    return MetaResponse(
        relay_enabled=settings.relay.enabled,
        default_memory_limit_mb=settings.memory_limit.default_mb,
        max_memory_limit_mb=settings.memory_limit.max_mb,
    )
