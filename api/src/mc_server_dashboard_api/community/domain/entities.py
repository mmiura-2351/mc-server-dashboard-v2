"""Entities for the community context.

Pure data with their invariants, standard-library only. ``Community`` is the
isolation/ownership unit (FR-COMM-1); ``Membership`` is the many-to-many join
between a user and a community (FR-MEM-2); ``Role`` is a community-scoped named
permission set (FR-AUTHZ-4); ``ResourceGrant`` is a per-resource permission set
granted to a member (FR-AUTHZ-2). The shapes mirror DATABASE.md Sections 5-6.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

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


@dataclass
class Community:
    """Row of the ``community`` table (DATABASE.md Section 5).

    ``max_servers`` / ``max_members`` are the room-for-quotas left by decision
    #9: nullable and unread by M1 business logic. They exist so a future
    milestone can enforce limits without a schema change.
    """

    id: CommunityId
    name: CommunityName
    created_at: dt.datetime
    updated_at: dt.datetime
    max_servers: int | None = None
    max_members: int | None = None


@dataclass
class Membership:
    """Row of the ``membership`` table: the (user, community) join (DATABASE.md 5).

    Unique per ``(user_id, community_id)`` — a user is a member of a community at
    most once. Roles held in the community attach to the membership, not the
    user, via ``membership_role``.
    """

    id: MembershipId
    user_id: UserId
    community_id: CommunityId
    created_at: dt.datetime


@dataclass
class Role:
    """Row of the ``role`` table: a community-scoped permission set (DATABASE.md 5).

    ``name`` is unique within the community. ``permissions`` is the set of
    ``<resource>:<action>`` codes the role grants. ``is_preset`` marks a seeded
    preset (e.g. Owner) versus an owner-defined role.
    """

    id: RoleId
    community_id: CommunityId
    name: RoleName
    permissions: set[Permission]
    created_at: dt.datetime
    updated_at: dt.datetime
    is_preset: bool = False


@dataclass
class ResourceGrant:
    """Row of the ``resource_grant`` table: a per-resource grant (DATABASE.md 6).

    A permission set granted to a member on a specific resource. Keyed by
    ``user_id`` (not membership) for natural querying; it also carries
    ``community_id`` so member-removal can sweep it (FR-MEM-3, Section 10).
    ``resource_id`` is a soft reference (no DB FK) because ``resource_type`` is
    polymorphic (``server`` in M1).
    """

    id: ResourceGrantId
    user_id: UserId
    community_id: CommunityId
    resource_type: str
    resource_id: uuid.UUID
    permissions: set[Permission]
    created_at: dt.datetime
    updated_at: dt.datetime
