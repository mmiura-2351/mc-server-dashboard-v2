"""Platform-admin community-oversight endpoint (issue #489).

The cross-cutting admin axis (FR-AUTHZ-5) gates this, the same posture as
``/users`` and ``/admin/users``: the route depends on ``require_platform_admin``
(non-admin -> 403). It is the platform-axis counterpart to ``GET /communities``,
which stays membership-scoped (FR-MEM-4): an admin sees *all* communities here,
regardless of membership (WEBUI_SPEC.md Section 3 persona).

Read-only and paginated (``limit``/``offset``, mirroring ``GET /users``); like
the other admin reads it records no audit event (only mutations do, e.g.
``admin_users.py``). Each row carries the operational counts the persistence
layer can supply cheaply in grouped queries: ``member_count`` and
``server_count`` (issue #489).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from mc_server_dashboard_api.community.application.list_all_communities import (
    ListAllCommunities,
)
from mc_server_dashboard_api.community.domain.entities import CommunitySummary
from mc_server_dashboard_api.dependencies import (
    get_list_all_communities,
    require_platform_admin,
)

router = APIRouter()


class AdminCommunityResponse(BaseModel):
    """Admin view of a community: identity, age, and its operational counts."""

    id: str
    name: str
    created_at: str
    member_count: int
    server_count: int

    @classmethod
    def from_summary(cls, summary: CommunitySummary) -> "AdminCommunityResponse":
        return cls(
            id=str(summary.id.value),
            name=summary.name.value,
            created_at=summary.created_at.isoformat(),
            member_count=summary.member_count,
            server_count=summary.server_count,
        )


class AdminCommunityListResponse(BaseModel):
    communities: list[AdminCommunityResponse]
    total: int
    limit: int
    offset: int


@router.get("/admin/communities", dependencies=[Depends(require_platform_admin)])
async def list_all_communities(
    use_case: Annotated[ListAllCommunities, Depends(get_list_all_communities)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminCommunityListResponse:
    page = await use_case(limit=limit, offset=offset)
    return AdminCommunityListResponse(
        communities=[AdminCommunityResponse.from_summary(c) for c in page.communities],
        total=page.total,
        limit=limit,
        offset=offset,
    )
