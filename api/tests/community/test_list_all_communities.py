"""Use-case tests for :class:`ListAllCommunities` (issue #489).

The platform-admin listing is membership-independent: it returns *every*
community with its member/server counts, paginated and ordered newest-first.
Exercised against the in-memory community fakes (no database, NFR-TEST-1).
"""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.community.application.list_all_communities import (
    ListAllCommunities,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    CommunityName,
)
from tests.community.fakes import FakeAuthzUnitOfWork


def _community(name: str, *, created_at: dt.datetime) -> Community:
    return Community(
        id=CommunityId.new(),
        name=CommunityName(name),
        created_at=created_at,
        updated_at=created_at,
    )


def _seed(
    uow: FakeAuthzUnitOfWork, community: Community, *, members: int, servers: int
) -> None:
    uow.communities.by_id[community.id] = community
    uow.communities.member_counts[community.id] = members
    uow.communities.server_counts[community.id] = servers


@pytest.mark.asyncio
async def test_lists_every_community_with_counts() -> None:
    uow = FakeAuthzUnitOfWork()
    older = _community(
        "older", created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    )
    newer = _community(
        "newer", created_at=dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    )
    _seed(uow, older, members=1, servers=0)
    _seed(uow, newer, members=4, servers=2)

    page = await ListAllCommunities(uow=uow)(limit=50, offset=0)

    assert page.total == 2
    # Newest first.
    assert [c.name.value for c in page.communities] == ["newer", "older"]
    assert page.communities[0].member_count == 4
    assert page.communities[0].server_count == 2
    assert page.communities[1].member_count == 1
    assert page.communities[1].server_count == 0


@pytest.mark.asyncio
async def test_pagination_slices_and_reports_total() -> None:
    uow = FakeAuthzUnitOfWork()
    for i in range(5):
        c = _community(
            f"c{i}", created_at=dt.datetime(2026, 1, 1 + i, tzinfo=dt.timezone.utc)
        )
        _seed(uow, c, members=0, servers=0)

    page = await ListAllCommunities(uow=uow)(limit=2, offset=2)

    assert page.total == 5
    assert len(page.communities) == 2
