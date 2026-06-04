"""identity: user and refresh_token tables

Creates the identity tables exactly per docs/app/DATABASE.md Section 4: a global
``user`` identity with case-insensitive username uniqueness and a
``refresh_token`` revocation/expiry record keyed to it (``ON DELETE CASCADE``),
with the indexes used by "revoke all sessions" and expiry sweeps.

Revision ID: 0002_identity
Revises: 0001_baseline
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_identity"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column(
            "is_platform_admin",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_user"),
        sa.UniqueConstraint("username", name="uq_user_username"),
        sa.UniqueConstraint("email", name="uq_user_email"),
    )
    # Case-insensitive username uniqueness (DATABASE.md Section 4): the plain
    # UNIQUE(username) above guards exact spellings; this functional index makes
    # two case-variant spellings collide as well.
    op.create_index(
        "uq_user_username_lower",
        "user",
        [sa.text("lower(username)")],
        unique=True,
    )

    op.create_table(
        "refresh_token",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_token"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_refresh_token_user_id_user",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("token_hash", name="uq_refresh_token_token_hash"),
    )
    # "Revoke all sessions" for a user (DATABASE.md Section 4).
    op.create_index(
        "ix_refresh_token_user_id",
        "refresh_token",
        ["user_id"],
    )
    # Expiry sweeps over still-live tokens (DATABASE.md Section 4: partial index
    # on expires_at for the not-yet-revoked rows).
    op.create_index(
        "ix_refresh_token_expires_at",
        "refresh_token",
        ["expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_refresh_token_expires_at", table_name="refresh_token")
    op.drop_index("ix_refresh_token_user_id", table_name="refresh_token")
    op.drop_table("refresh_token")
    op.drop_index("uq_user_username_lower", table_name="user")
    op.drop_table("user")
