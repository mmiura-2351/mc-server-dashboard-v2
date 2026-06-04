"""Unit tests for the read / rename / delete / list-my community use cases.

Against the in-memory fakes (TESTING.md Section 4). The authorization gate lives
in the route dependency, so these only verify the data behaviour: read/rename/
delete on a missing community raise ``CommunityNotFoundError``; rename mutates
the name and rejects a clashing one; list-my returns only the user's communities.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.community.application.list_my_communities import (
    ListMyCommunities,
)
from mc_server_dashboard_api.community.application.manage_community import (
    DeleteCommunity,
    ReadCommunity,
    RenameCommunity,
)
from mc_server_dashboard_api.community.domain.clock import Clock
from mc_server_dashboard_api.community.domain.entities import Community, Membership
from mc_server_dashboard_api.community.domain.errors import (
    CommunityAlreadyExistsError,
    CommunityNotFoundError,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    CommunityName,
    MembershipId,
    UserId,
)
from tests.community.fakes import FakeAuthzUnitOfWork

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


class _FakeClock(Clock):
    def now(self) -> dt.datetime:
        return _NOW


def _seed_community(uow: FakeAuthzUnitOfWork, name: str = "guild") -> Community:
    community = Community(
        id=CommunityId.new(),
        name=CommunityName(name),
        created_at=_NOW,
        updated_at=_NOW,
    )
    uow.communities.by_id[community.id] = community
    return community


async def test_read_returns_existing_community() -> None:
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow)
    loaded = await ReadCommunity(uow=uow)(community_id=community.id)
    assert loaded.id == community.id


async def test_read_missing_raises_not_found() -> None:
    uow = FakeAuthzUnitOfWork()
    with pytest.raises(CommunityNotFoundError):
        await ReadCommunity(uow=uow)(community_id=CommunityId(uuid.uuid4()))


async def test_rename_changes_name_and_commits() -> None:
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow, "old")
    renamed = await RenameCommunity(uow=uow, clock=_FakeClock())(
        community_id=community.id, name="new"
    )
    assert renamed.name == CommunityName("new")
    assert uow.communities.by_id[community.id].name == CommunityName("new")
    assert uow.commits == 1


async def test_rename_missing_raises_not_found() -> None:
    uow = FakeAuthzUnitOfWork()
    with pytest.raises(CommunityNotFoundError):
        await RenameCommunity(uow=uow, clock=_FakeClock())(
            community_id=CommunityId(uuid.uuid4()), name="new"
        )


async def test_rename_to_existing_other_name_conflicts() -> None:
    uow = FakeAuthzUnitOfWork()
    _seed_community(uow, "taken")
    target = _seed_community(uow, "mine")
    with pytest.raises(CommunityAlreadyExistsError):
        await RenameCommunity(uow=uow, clock=_FakeClock())(
            community_id=target.id, name="taken"
        )


async def test_rename_to_same_name_is_allowed() -> None:
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow, "guild")
    renamed = await RenameCommunity(uow=uow, clock=_FakeClock())(
        community_id=community.id, name="guild"
    )
    assert renamed.name == CommunityName("guild")


async def test_delete_removes_community_and_commits() -> None:
    uow = FakeAuthzUnitOfWork()
    community = _seed_community(uow)
    await DeleteCommunity(uow=uow)(community_id=community.id)
    assert community.id not in uow.communities.by_id
    assert uow.commits == 1


async def test_delete_missing_raises_not_found() -> None:
    uow = FakeAuthzUnitOfWork()
    with pytest.raises(CommunityNotFoundError):
        await DeleteCommunity(uow=uow)(community_id=CommunityId(uuid.uuid4()))


async def test_list_my_communities_scopes_to_member() -> None:
    uow = FakeAuthzUnitOfWork()
    user = UserId(uuid.uuid4())
    other = UserId(uuid.uuid4())
    mine = _seed_community(uow, "mine")
    theirs = _seed_community(uow, "theirs")
    uow.memberships.by_id[MembershipId.new()] = Membership(
        id=MembershipId.new(),
        user_id=user,
        community_id=mine.id,
        created_at=_NOW,
    )
    uow.memberships.by_id[MembershipId.new()] = Membership(
        id=MembershipId.new(),
        user_id=other,
        community_id=theirs.id,
        created_at=_NOW,
    )

    listed = await ListMyCommunities(uow=uow)(user_id=user)
    assert [c.id for c in listed] == [mine.id]


async def test_list_my_communities_empty_for_non_member() -> None:
    uow = FakeAuthzUnitOfWork()
    _seed_community(uow)
    listed = await ListMyCommunities(uow=uow)(user_id=UserId(uuid.uuid4()))
    assert listed == []
