"""Entity isolation across the community repository fakes (#2516).

The servers context established the rule in
:mod:`tests.servers.test_server_repository_fake` (#2505, PR #2512) and PR #2548
generalised it across the servers and identity fakes; the community fakes carry
the same defect and get the same answer. Restated: a real adapter is a one-way
seam in both directions -- a writer serializes the entity into an INSERT/UPDATE,
so no later in-memory edit can reach the row, and a reader materializes a fresh
entity per SELECT, so an edit to a loaded one reaches the database only through a
writer.

A fake that stores, or hands back, the caller's object is therefore *more
forgiving than production*, and only in that direction: a mutant that should
redden a persisted-state assertion can be absorbed by the aliasing with nothing
visible in the test source. So each test here *demonstrates* the detachment
rather than asserting the copy exists: it mutates the entity on the caller's
side of the boundary after the call and shows the other side did not move.

``Role`` and ``ResourceGrant`` carry a ``permissions`` set of frozen
``Permission`` values -- the only mutable field on any community entity -- so the
mutation goes one level in (``permissions.add(...)``): a shared set that claims a
detachment it does not have reddens too. ``Community`` and ``Membership`` carry
no mutable field, so a new entity is the full copy depth.
"""

from __future__ import annotations

import datetime as dt
import uuid

from mc_server_dashboard_api.community.domain.entities import (
    Community,
    Membership,
    ResourceGrant,
    Role,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    CommunityName,
    MembershipId,
    Permission,
    ResourceGrantId,
    RoleId,
    RoleName,
    UserId,
)
from tests.community.fakes import (
    FakeCommunityRepository,
    FakeMembershipRepository,
    FakeResourceGrantRepository,
    FakeRoleRepository,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.UTC)
_COMMUNITY = CommunityId.new()
_USER = UserId(uuid.uuid4())


# -- FakeCommunityRepository --


def _community() -> Community:
    return Community(
        id=CommunityId.new(),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
        max_servers=None,
        max_members=None,
    )


def test_community_seed_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeCommunityRepository()
    community = _community()

    repo.seed(community)

    stored = repo.by_id[community.id]
    community.name = CommunityName("rewritten")
    community.max_servers = 99
    assert stored.name == CommunityName("guild")
    assert stored.max_servers is None


async def test_community_add_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeCommunityRepository()
    community = _community()

    await repo.add(community)

    stored = repo.by_id[community.id]
    community.name = CommunityName("rewritten")
    assert stored.name == CommunityName("guild")


async def test_community_update_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeCommunityRepository()
    community = _community()
    repo.seed(community)
    community.name = CommunityName("renamed")

    await repo.update(community)

    stored = repo.by_id[community.id]
    community.name = CommunityName("after-the-write")
    assert stored.name == CommunityName("renamed")


async def test_community_readers_hand_out_copies() -> None:
    repo = FakeCommunityRepository()
    community = _community()
    repo.seed(community)

    loaded = [
        await repo.get_by_id(community.id),
        await repo.get_by_name(community.name),
    ]

    for handed_out in loaded:
        assert handed_out is not None
        handed_out.name = CommunityName("rewritten")
    assert repo.by_id[community.id].name == CommunityName("guild")


# -- FakeMembershipRepository --


def _membership() -> Membership:
    return Membership(
        id=MembershipId.new(),
        user_id=_USER,
        community_id=_COMMUNITY,
        created_at=_NOW,
    )


def test_membership_seed_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeMembershipRepository()
    membership = _membership()

    repo.seed(membership)

    stored = repo.by_id[membership.id]
    membership.community_id = CommunityId.new()
    membership.created_at = _NOW + dt.timedelta(days=1)
    assert stored.community_id == _COMMUNITY
    assert stored.created_at == _NOW


async def test_membership_add_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeMembershipRepository()
    membership = _membership()

    await repo.add(membership)

    stored = repo.by_id[membership.id]
    membership.community_id = CommunityId.new()
    assert stored.community_id == _COMMUNITY


async def test_membership_readers_hand_out_copies() -> None:
    repo = FakeMembershipRepository()
    membership = _membership()
    repo.seed(membership)

    loaded = [
        await repo.get_by_id(membership.id),
        await repo.get_by_user_and_community(_USER, _COMMUNITY),
        (await repo.list_for_user(_USER))[0],
        (await repo.list_for_community(_COMMUNITY))[0],
    ]

    for handed_out in loaded:
        assert handed_out is not None
        handed_out.community_id = CommunityId.new()
    assert repo.by_id[membership.id].community_id == _COMMUNITY


# -- FakeRoleRepository --


def _role() -> Role:
    return Role(
        id=RoleId.new(),
        community_id=_COMMUNITY,
        name=RoleName("Editor"),
        permissions={Permission("server:read")},
        created_at=_NOW,
        updated_at=_NOW,
    )


def _rewrite_role(role: Role) -> None:
    role.name = RoleName("rewritten")
    role.permissions.add(Permission("server:delete"))


def _assert_role_unchanged(role: Role) -> None:
    assert role.name == RoleName("Editor")
    assert role.permissions == {Permission("server:read")}


def test_role_seed_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeRoleRepository()
    role = _role()

    repo.seed(role)

    stored = repo.by_id[role.id]
    _rewrite_role(role)
    _assert_role_unchanged(stored)


async def test_role_add_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeRoleRepository()
    role = _role()

    await repo.add(role)

    stored = repo.by_id[role.id]
    _rewrite_role(role)
    _assert_role_unchanged(stored)


async def test_role_update_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeRoleRepository()
    role = _role()
    repo.seed(role)

    await repo.update(role)

    stored = repo.by_id[role.id]
    _rewrite_role(role)
    _assert_role_unchanged(stored)


async def test_role_readers_hand_out_copies() -> None:
    repo = FakeRoleRepository()
    role = _role()
    repo.seed(role)

    loaded = [
        await repo.get_by_id(role.id),
        (await repo.get_by_ids([role.id]))[0],
        (await repo.list_for_community(_COMMUNITY))[0],
    ]

    for handed_out in loaded:
        assert handed_out is not None
        _rewrite_role(handed_out)
    _assert_role_unchanged(repo.by_id[role.id])


# -- FakeResourceGrantRepository --


def _grant() -> ResourceGrant:
    return ResourceGrant(
        id=ResourceGrantId.new(),
        user_id=_USER,
        community_id=_COMMUNITY,
        resource_type="server",
        resource_id=uuid.uuid4(),
        permissions={Permission("server:read")},
        created_at=_NOW,
        updated_at=_NOW,
    )


def _rewrite_grant(grant: ResourceGrant) -> None:
    grant.resource_type = "rewritten"
    grant.permissions.add(Permission("server:delete"))


def _assert_grant_unchanged(grant: ResourceGrant) -> None:
    assert grant.resource_type == "server"
    assert grant.permissions == {Permission("server:read")}


async def test_grant_add_stores_a_copy_the_caller_cannot_rewrite() -> None:
    repo = FakeResourceGrantRepository()
    grant = _grant()

    await repo.add(grant)

    stored = repo.by_id[grant.id]
    _rewrite_grant(grant)
    _assert_grant_unchanged(stored)


async def test_grant_readers_hand_out_copies() -> None:
    repo = FakeResourceGrantRepository()
    grant = _grant()
    repo.seed(grant)

    loaded = [
        await repo.get_by_id(grant.id),
        await repo.get_for_user_resource(
            _USER, _COMMUNITY, grant.resource_type, grant.resource_id
        ),
        (await repo.list_for_community(_COMMUNITY))[0],
    ]

    for handed_out in loaded:
        assert handed_out is not None
        _rewrite_grant(handed_out)
    _assert_grant_unchanged(repo.by_id[grant.id])
