"""ListAllCommunities use case: the platform-admin community listing (issue #489).

The platform-admin axis sees *all* communities (WEBUI_SPEC.md Section 3 persona),
unlike :class:`ListMyCommunities`, which is membership-scoped. This reads a page of
communities ordered by ``created_at`` plus the total count, each row enriched with
its member/server counts. Read-only: it opens the unit of work, queries, and never
commits. The gate (``require_platform_admin``) is applied at the HTTP edge.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.community.domain.entities import CommunitySummary
from mc_server_dashboard_api.community.domain.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class CommunityPage:
    """A page of community summaries plus the total row count (for pagination)."""

    communities: list[CommunitySummary]
    total: int


@dataclass(frozen=True)
class ListAllCommunities:
    """List every community for the platform-admin oversight surface (#489)."""

    uow: UnitOfWork

    async def __call__(self, *, limit: int, offset: int) -> CommunityPage:
        async with self.uow:
            communities = await self.uow.communities.list_summaries_page(
                limit=limit, offset=offset
            )
            total = await self.uow.communities.count_all()
        return CommunityPage(communities=communities, total=total)
