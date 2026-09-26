"""Reusable contracts for the four community persistence Ports (#3135).

Every case uses only the public repository interface and runs unchanged against
the in-memory fake and PostgreSQL adapter.  The surrounding test modules choose
the implementation and lane; this module defines the observable behavior once.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

import pytest

from mc_server_dashboard_api.community.domain.entities import (
    Community,
    Membership,
    ResourceGrant,
    Role,
)
from mc_server_dashboard_api.community.domain.errors import (
    CommunityAlreadyExistsError,
    CommunityNotFoundError,
    MembershipAlreadyExistsError,
    ResourceGrantAlreadyExistsError,
    RoleAlreadyExistsError,
    RoleNotFoundError,
)
from mc_server_dashboard_api.community.domain.repositories import (
    CommunityRepository,
    MembershipRepository,
    ResourceGrantRepository,
    RoleRepository,
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
from tests.contracts.repository import RepositoryHarness

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.UTC)


@dataclass(frozen=True)
class MembershipRepositoryHarness(RepositoryHarness[MembershipRepository]):
    user_id: UserId
    other_user_id: UserId
    community_id: CommunityId
    other_community_id: CommunityId


@dataclass(frozen=True)
class RoleRepositoryHarness(RepositoryHarness[RoleRepository]):
    community_id: CommunityId
    other_community_id: CommunityId


@dataclass(frozen=True)
class ResourceGrantRepositoryHarness(RepositoryHarness[ResourceGrantRepository]):
    user_id: UserId
    other_user_id: UserId
    community_id: CommunityId
    other_community_id: CommunityId


def _community(name: str = "guild") -> Community:
    return Community(
        id=CommunityId.new(),
        name=CommunityName(name),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _membership(
    harness: MembershipRepositoryHarness,
    *,
    user_id: UserId | None = None,
    community_id: CommunityId | None = None,
) -> Membership:
    return Membership(
        id=MembershipId.new(),
        user_id=user_id or harness.user_id,
        community_id=community_id or harness.community_id,
        created_at=_NOW,
    )


def _role(
    harness: RoleRepositoryHarness,
    name: str = "Editor",
    *,
    community_id: CommunityId | None = None,
) -> Role:
    return Role(
        id=RoleId.new(),
        community_id=community_id or harness.community_id,
        name=RoleName(name),
        permissions={Permission("server:read")},
        created_at=_NOW,
        updated_at=_NOW,
    )


def _grant(
    harness: ResourceGrantRepositoryHarness,
    *,
    user_id: UserId | None = None,
    community_id: CommunityId | None = None,
    resource_id: uuid.UUID | None = None,
) -> ResourceGrant:
    return ResourceGrant(
        id=ResourceGrantId.new(),
        user_id=user_id or harness.user_id,
        community_id=community_id or harness.community_id,
        resource_type="server",
        resource_id=resource_id or uuid.uuid4(),
        permissions={Permission("server:read")},
        created_at=_NOW,
        updated_at=_NOW,
    )


class CommunityRepositoryContract:
    """Observable contract promised by :class:`CommunityRepository`."""

    async def test_add_detaches_the_persisted_values(
        self, community_repository_harness: RepositoryHarness[CommunityRepository]
    ) -> None:
        community = _community()
        async with community_repository_harness.open() as transaction:
            await transaction.repository.add(community)
            community.name = CommunityName("rewritten-before-commit")
            community.max_servers = 99
            await transaction.commit()

        async with community_repository_harness.open() as transaction:
            loaded = await transaction.repository.get_by_id(community.id)

        assert loaded is not None
        assert loaded.name == CommunityName("guild")
        assert loaded.max_servers is None

    async def test_readers_return_detached_entities_and_none_for_missing_rows(
        self, community_repository_harness: RepositoryHarness[CommunityRepository]
    ) -> None:
        community = _community()
        async with community_repository_harness.open() as transaction:
            await transaction.repository.add(community)
            await transaction.commit()

        async with community_repository_harness.open() as transaction:
            by_id = await transaction.repository.get_by_id(community.id)
            by_name = await transaction.repository.get_by_name(community.name)
            missing_id = await transaction.repository.get_by_id(CommunityId.new())
            missing_name = await transaction.repository.get_by_name(
                CommunityName("missing")
            )
            assert by_id is not None
            assert by_name is not None
            by_id.name = CommunityName("rewritten-by-id")
            by_name.max_members = 42

        async with community_repository_harness.open() as transaction:
            reloaded = await transaction.repository.get_by_id(community.id)

        assert reloaded is not None
        assert reloaded.name == CommunityName("guild")
        assert reloaded.max_members is None
        assert missing_id is None
        assert missing_name is None

    async def test_update_changes_only_an_existing_row_and_detaches_the_write(
        self, community_repository_harness: RepositoryHarness[CommunityRepository]
    ) -> None:
        community = _community()
        async with community_repository_harness.open() as transaction:
            await transaction.repository.add(community)
            await transaction.commit()

        community.name = CommunityName("renamed")
        community.updated_at = _NOW + dt.timedelta(hours=1)
        community.created_at = _NOW + dt.timedelta(days=1)
        community.max_servers = 99
        community.max_members = 42
        async with community_repository_harness.open() as transaction:
            await transaction.repository.update(community)
            community.name = CommunityName("rewritten-after-update")
            await transaction.commit()

        async with community_repository_harness.open() as transaction:
            loaded = await transaction.repository.get_by_id(community.id)

        assert loaded is not None
        assert loaded.name == CommunityName("renamed")
        assert loaded.updated_at == _NOW + dt.timedelta(hours=1)
        assert loaded.created_at == _NOW
        assert loaded.max_servers is None
        assert loaded.max_members is None

    async def test_update_of_missing_row_reports_not_found_without_inserting(
        self, community_repository_harness: RepositoryHarness[CommunityRepository]
    ) -> None:
        community = _community()
        with pytest.raises(CommunityNotFoundError):
            async with community_repository_harness.open() as transaction:
                await transaction.repository.update(community)
                await transaction.commit()

        async with community_repository_harness.open() as transaction:
            assert await transaction.repository.get_by_id(community.id) is None

    async def test_name_is_unique_for_add_and_update(
        self, community_repository_harness: RepositoryHarness[CommunityRepository]
    ) -> None:
        taken = _community("taken")
        renamed = _community("free")
        async with community_repository_harness.open() as transaction:
            await transaction.repository.add(taken)
            await transaction.repository.add(renamed)
            await transaction.commit()

        with pytest.raises(CommunityAlreadyExistsError):
            async with community_repository_harness.open() as transaction:
                await transaction.repository.add(_community("taken"))
                await transaction.commit()

        renamed.name = CommunityName("taken")
        with pytest.raises(CommunityAlreadyExistsError):
            async with community_repository_harness.open() as transaction:
                await transaction.repository.update(renamed)
                await transaction.commit()


class MembershipRepositoryContract:
    """Observable contract promised by :class:`MembershipRepository`."""

    async def test_add_detaches_the_persisted_values(
        self, membership_repository_harness: MembershipRepositoryHarness
    ) -> None:
        membership = _membership(membership_repository_harness)
        async with membership_repository_harness.open() as transaction:
            await transaction.repository.add(membership)
            membership.community_id = membership_repository_harness.other_community_id
            membership.created_at = _NOW + dt.timedelta(days=1)
            await transaction.commit()

        async with membership_repository_harness.open() as transaction:
            loaded = await transaction.repository.get_by_id(membership.id)

        assert loaded is not None
        assert loaded.community_id == membership_repository_harness.community_id
        assert loaded.created_at == _NOW

    async def test_readers_return_detached_entities_and_scope_their_results(
        self, membership_repository_harness: MembershipRepositoryHarness
    ) -> None:
        target = _membership(membership_repository_harness)
        other = _membership(
            membership_repository_harness,
            user_id=membership_repository_harness.other_user_id,
            community_id=membership_repository_harness.other_community_id,
        )
        async with membership_repository_harness.open() as transaction:
            await transaction.repository.add(target)
            await transaction.repository.add(other)
            await transaction.commit()

        async with membership_repository_harness.open() as transaction:
            by_id = await transaction.repository.get_by_id(target.id)
            by_pair = await transaction.repository.get_by_user_and_community(
                target.user_id, target.community_id
            )
            for_user = await transaction.repository.list_for_user(target.user_id)
            for_community = await transaction.repository.list_for_community(
                target.community_id
            )
            assert by_id is not None
            assert by_pair is not None
            assert [row.id for row in for_user] == [target.id]
            assert [row.id for row in for_community] == [target.id]
            for handed_out in (by_id, by_pair, for_user[0], for_community[0]):
                handed_out.community_id = (
                    membership_repository_harness.other_community_id
                )

        async with membership_repository_harness.open() as transaction:
            reloaded = await transaction.repository.get_by_id(target.id)
            missing_id = await transaction.repository.get_by_id(MembershipId.new())
            missing_pair = await transaction.repository.get_by_user_and_community(
                membership_repository_harness.user_id,
                membership_repository_harness.other_community_id,
            )

        assert reloaded is not None
        assert reloaded.community_id == membership_repository_harness.community_id
        assert missing_id is None
        assert missing_pair is None

    async def test_user_and_community_pair_is_unique(
        self, membership_repository_harness: MembershipRepositoryHarness
    ) -> None:
        async with membership_repository_harness.open() as transaction:
            await transaction.repository.add(_membership(membership_repository_harness))
            await transaction.commit()

        with pytest.raises(MembershipAlreadyExistsError):
            async with membership_repository_harness.open() as transaction:
                await transaction.repository.add(
                    _membership(membership_repository_harness)
                )
                await transaction.commit()


class RoleRepositoryContract:
    """Observable contract promised by :class:`RoleRepository`."""

    async def test_add_detaches_mutable_permissions(
        self, role_repository_harness: RoleRepositoryHarness
    ) -> None:
        role = _role(role_repository_harness)
        async with role_repository_harness.open() as transaction:
            await transaction.repository.add(role)
            role.name = RoleName("rewritten-before-commit")
            role.permissions.add(Permission("server:delete"))
            await transaction.commit()

        async with role_repository_harness.open() as transaction:
            loaded = await transaction.repository.get_by_id(role.id)

        assert loaded is not None
        assert loaded.name == RoleName("Editor")
        assert loaded.permissions == {Permission("server:read")}

    async def test_readers_return_detached_entities_and_scope_their_results(
        self, role_repository_harness: RoleRepositoryHarness
    ) -> None:
        target = _role(role_repository_harness)
        other = _role(
            role_repository_harness,
            name="Other",
            community_id=role_repository_harness.other_community_id,
        )
        async with role_repository_harness.open() as transaction:
            await transaction.repository.add(target)
            await transaction.repository.add(other)
            await transaction.commit()

        async with role_repository_harness.open() as transaction:
            by_id = await transaction.repository.get_by_id(target.id)
            by_ids = await transaction.repository.get_by_ids([target.id, RoleId.new()])
            listed = await transaction.repository.list_for_community(
                role_repository_harness.community_id
            )
            empty = await transaction.repository.get_by_ids([])
            assert by_id is not None
            assert [row.id for row in by_ids] == [target.id]
            assert [row.id for row in listed] == [target.id]
            assert empty == []
            for handed_out in (by_id, by_ids[0], listed[0]):
                handed_out.name = RoleName("rewritten")
                handed_out.permissions.add(Permission("server:delete"))

        async with role_repository_harness.open() as transaction:
            reloaded = await transaction.repository.get_by_id(target.id)
            missing = await transaction.repository.get_by_id(RoleId.new())

        assert reloaded is not None
        assert reloaded.name == RoleName("Editor")
        assert reloaded.permissions == {Permission("server:read")}
        assert missing is None

    async def test_update_changes_only_an_existing_row_and_detaches_the_write(
        self, role_repository_harness: RoleRepositoryHarness
    ) -> None:
        role = _role(role_repository_harness)
        async with role_repository_harness.open() as transaction:
            await transaction.repository.add(role)
            await transaction.commit()

        role.name = RoleName("Moderator")
        role.permissions = {Permission("server:read"), Permission("server:start")}
        role.updated_at = _NOW + dt.timedelta(hours=1)
        role.community_id = role_repository_harness.other_community_id
        role.created_at = _NOW + dt.timedelta(days=1)
        role.is_preset = True
        async with role_repository_harness.open() as transaction:
            await transaction.repository.update(role)
            role.name = RoleName("rewritten-after-update")
            role.permissions.add(Permission("server:delete"))
            await transaction.commit()

        async with role_repository_harness.open() as transaction:
            loaded = await transaction.repository.get_by_id(role.id)

        assert loaded is not None
        assert loaded.name == RoleName("Moderator")
        assert loaded.permissions == {
            Permission("server:read"),
            Permission("server:start"),
        }
        assert loaded.updated_at == _NOW + dt.timedelta(hours=1)
        assert loaded.community_id == role_repository_harness.community_id
        assert loaded.created_at == _NOW
        assert loaded.is_preset is False

    async def test_update_of_missing_row_reports_not_found_without_inserting(
        self, role_repository_harness: RoleRepositoryHarness
    ) -> None:
        role = _role(role_repository_harness)
        with pytest.raises(RoleNotFoundError):
            async with role_repository_harness.open() as transaction:
                await transaction.repository.update(role)
                await transaction.commit()

        async with role_repository_harness.open() as transaction:
            assert await transaction.repository.get_by_id(role.id) is None

    async def test_name_is_unique_within_a_community_for_add_and_update(
        self, role_repository_harness: RoleRepositoryHarness
    ) -> None:
        taken = _role(role_repository_harness, "Owner")
        renamed = _role(role_repository_harness, "Editor")
        same_name_other_community = _role(
            role_repository_harness,
            "Owner",
            community_id=role_repository_harness.other_community_id,
        )
        async with role_repository_harness.open() as transaction:
            await transaction.repository.add(taken)
            await transaction.repository.add(renamed)
            await transaction.repository.add(same_name_other_community)
            await transaction.commit()

        with pytest.raises(RoleAlreadyExistsError):
            async with role_repository_harness.open() as transaction:
                await transaction.repository.add(
                    _role(role_repository_harness, "Owner")
                )
                await transaction.commit()

        renamed.name = RoleName("Owner")
        with pytest.raises(RoleAlreadyExistsError):
            async with role_repository_harness.open() as transaction:
                await transaction.repository.update(renamed)
                await transaction.commit()


class ResourceGrantRepositoryContract:
    """Observable contract promised by :class:`ResourceGrantRepository`."""

    async def test_add_detaches_mutable_permissions(
        self, resource_grant_repository_harness: ResourceGrantRepositoryHarness
    ) -> None:
        grant = _grant(resource_grant_repository_harness)
        original_resource_id = grant.resource_id
        async with resource_grant_repository_harness.open() as transaction:
            await transaction.repository.add(grant)
            grant.resource_id = uuid.uuid4()
            grant.permissions.add(Permission("server:delete"))
            await transaction.commit()

        async with resource_grant_repository_harness.open() as transaction:
            loaded = await transaction.repository.get_by_id(grant.id)

        assert loaded is not None
        assert loaded.resource_id == original_resource_id
        assert loaded.permissions == {Permission("server:read")}

    async def test_readers_return_detached_entities_and_enforce_full_scope(
        self, resource_grant_repository_harness: ResourceGrantRepositoryHarness
    ) -> None:
        target = _grant(resource_grant_repository_harness)
        other = _grant(
            resource_grant_repository_harness,
            user_id=resource_grant_repository_harness.other_user_id,
            community_id=resource_grant_repository_harness.other_community_id,
        )
        async with resource_grant_repository_harness.open() as transaction:
            await transaction.repository.add(target)
            await transaction.repository.add(other)
            await transaction.commit()

        async with resource_grant_repository_harness.open() as transaction:
            by_id = await transaction.repository.get_by_id(target.id)
            by_key = await transaction.repository.get_for_user_resource(
                target.user_id,
                target.community_id,
                target.resource_type,
                target.resource_id,
            )
            listed = await transaction.repository.list_for_community(
                target.community_id
            )
            listed_for_user = await transaction.repository.list_for_community(
                target.community_id, target.user_id
            )
            cross_community = await transaction.repository.get_for_user_resource(
                target.user_id,
                resource_grant_repository_harness.other_community_id,
                target.resource_type,
                target.resource_id,
            )
            assert by_id is not None
            assert by_key is not None
            assert [row.id for row in listed] == [target.id]
            assert [row.id for row in listed_for_user] == [target.id]
            assert cross_community is None
            for handed_out in (by_id, by_key, listed[0], listed_for_user[0]):
                handed_out.permissions.add(Permission("server:delete"))

        async with resource_grant_repository_harness.open() as transaction:
            reloaded = await transaction.repository.get_by_id(target.id)
            missing = await transaction.repository.get_by_id(ResourceGrantId.new())

        assert reloaded is not None
        assert reloaded.permissions == {Permission("server:read")}
        assert missing is None

    async def test_user_resource_key_is_unique(
        self, resource_grant_repository_harness: ResourceGrantRepositoryHarness
    ) -> None:
        original = _grant(resource_grant_repository_harness)
        async with resource_grant_repository_harness.open() as transaction:
            await transaction.repository.add(original)
            await transaction.commit()

        duplicate = _grant(
            resource_grant_repository_harness,
            community_id=resource_grant_repository_harness.other_community_id,
            resource_id=original.resource_id,
        )
        with pytest.raises(ResourceGrantAlreadyExistsError):
            async with resource_grant_repository_harness.open() as transaction:
                await transaction.repository.add(duplicate)
                await transaction.commit()
