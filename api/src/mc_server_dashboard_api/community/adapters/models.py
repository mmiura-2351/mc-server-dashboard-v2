"""SQLAlchemy ORM models for the community tables (DATABASE.md Sections 5-6).

These map the ``community``, ``membership``, ``role``, ``membership_role`` and
``resource_grant`` tables. They are an adapter detail: the domain entities
(``domain.entities``) are framework-free, and these models are translated
to/from them in the repositories. The ``user_id`` columns FK the identity
``user`` table at the persistence layer (DATABASE.md Section 5); the domain side
holds only the id value.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from mc_server_dashboard_api.core.adapters.database import Base


class CommunityModel(Base):
    """Row of the ``community`` table (DATABASE.md Section 5)."""

    __tablename__ = "community"
    __table_args__ = (UniqueConstraint("name", name="uq_community_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    max_servers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_members: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class MembershipModel(Base):
    """Row of the ``membership`` table (DATABASE.md Section 5)."""

    __tablename__ = "membership"
    __table_args__ = (
        # A user is a member of a community at most once (DATABASE.md Section 5).
        UniqueConstraint(
            "user_id", "community_id", name="uq_membership_user_community"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    community_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("community.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RoleModel(Base):
    """Row of the ``role`` table: a community-scoped permission set (DATABASE.md 5)."""

    __tablename__ = "role"
    __table_args__ = (
        # Names are unique per community, not globally (DATABASE.md Section 5).
        UniqueConstraint("community_id", "name", name="uq_role_community_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    community_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("community.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    is_preset: Mapped[bool] = mapped_column(nullable=False, server_default=false())
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class MembershipRoleModel(Base):
    """Row of the ``membership_role`` join: roles assigned to a membership (5)."""

    __tablename__ = "membership_role"

    membership_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("membership.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("role.id", ondelete="CASCADE"),
        primary_key=True,
    )


class ResourceGrantModel(Base):
    """Row of the ``resource_grant`` table: a per-resource grant (DATABASE.md 6).

    ``resource_id`` has no FK by design: ``resource_type`` is polymorphic, so the
    reference is soft and cleaned up by use cases (Section 10).
    """

    __tablename__ = "resource_grant"
    __table_args__ = (
        # One grant row per member per resource (DATABASE.md Section 6).
        UniqueConstraint(
            "user_id",
            "resource_type",
            "resource_id",
            name="uq_resource_grant_user_resource",
        ),
        # ``resource_type`` is a CHECK-constrained enum; ``server`` in M1. Bare
        # name; the ``ck`` naming convention renders
        # ``ck_resource_grant_resource_type`` (issue #60), matching the migration.
        CheckConstraint("resource_type IN ('server')", name="resource_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    community_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("community.id", ondelete="CASCADE"),
        nullable=False,
    )
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
