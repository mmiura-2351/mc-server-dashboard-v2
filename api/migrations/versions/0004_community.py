"""community: community, membership, role, membership_role, resource_grant

Creates the community tables exactly per docs/app/DATABASE.md Sections 5-6: the
``community`` ownership unit (with the nullable, M1-unused ``max_*`` quota
columns), the ``membership`` (user, community) join unique per pair, the
community-scoped ``role`` with names unique per community, the
``membership_role`` join, and the polymorphic ``resource_grant`` (no FK on
``resource_id`` by design). FK ``ON DELETE CASCADE`` behaviors implement the
community-delete and member-removal cascades documented in Section 10; the
``user_id`` FKs target the identity ``user`` table.

Revision ID: 0004_community
Revises: 0003_brute_force
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_community"
down_revision: str | None = "0003_brute_force"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "community",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("max_servers", sa.Integer(), nullable=True),
        sa.Column("max_members", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_community"),
        sa.UniqueConstraint("name", name="uq_community_name"),
    )

    op.create_table(
        "membership",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("community_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_membership"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_membership_user_id_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["community_id"],
            ["community.id"],
            name="fk_membership_community_id_community",
            ondelete="CASCADE",
        ),
        # A user is a member of a community at most once (DATABASE.md Section 5).
        sa.UniqueConstraint(
            "user_id", "community_id", name="uq_membership_user_community"
        ),
    )

    op.create_table(
        "role",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("community_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("permissions", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column(
            "is_preset",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_role"),
        sa.ForeignKeyConstraint(
            ["community_id"],
            ["community.id"],
            name="fk_role_community_id_community",
            ondelete="CASCADE",
        ),
        # Role names are unique per community, not globally (DATABASE.md 5).
        sa.UniqueConstraint("community_id", "name", name="uq_role_community_name"),
    )

    op.create_table(
        "membership_role",
        sa.Column("membership_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("membership_id", "role_id", name="pk_membership_role"),
        sa.ForeignKeyConstraint(
            ["membership_id"],
            ["membership.id"],
            name="fk_membership_role_membership_id_membership",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["role.id"],
            name="fk_membership_role_role_id_role",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "resource_grant",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("community_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        # No FK on resource_id by design: resource_type is polymorphic, so the
        # reference is soft and swept by use cases (DATABASE.md Sections 6, 10).
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permissions", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_resource_grant"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_resource_grant_user_id_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["community_id"],
            ["community.id"],
            name="fk_resource_grant_community_id_community",
            ondelete="CASCADE",
        ),
        # One grant row per member per resource (DATABASE.md Section 6).
        sa.UniqueConstraint(
            "user_id",
            "resource_type",
            "resource_id",
            name="uq_resource_grant_user_resource",
        ),
        # resource_type is a CHECK-constrained enum; server in M1 (DATABASE.md 6).
        sa.CheckConstraint(
            "resource_type IN ('server')",
            name="ck_resource_grant_resource_type",
        ),
    )


def downgrade() -> None:
    op.drop_table("resource_grant")
    op.drop_table("membership_role")
    op.drop_table("role")
    op.drop_table("membership")
    op.drop_table("community")
