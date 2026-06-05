"""servers: player groups (op / whitelist) + server attachments

Creates the three tables for player groups (issue #276), per
docs/app/DATABASE.md Section 7:

- ``player_group`` — a community-scoped group of one ``kind`` (op / whitelist),
  ``UNIQUE(community_id, kind, name)`` with a ``kind`` CHECK enum and a
  community FK (``ON DELETE CASCADE``).
- ``group_player`` — a player (uuid + username) under a group,
  ``UNIQUE(group_id, player_uuid)`` (the upsert key); ``ON DELETE CASCADE`` from
  its group.
- ``server_group`` — the many-to-many attachment join (composite PK
  ``(group_id, server_id)``); rows cascade when either the group or the server is
  deleted.

It also backfills the two new permission codes (``group:read`` / ``group:manage``)
onto every existing **preset** Owner role, mirroring 0008's audit:read backfill:
the codes were added to the catalog (the seed for *new* communities' Owner role),
so already-provisioned communities' Owner roles lack them and a data backfill
grants them. Only preset Owner rows (``is_preset = true`` AND ``name = 'Owner'``)
are touched; non-preset and non-Owner roles are left alone. Idempotent both ways.

Revision ID: 0012_player_groups
Revises: 0011_user_active
Create Date: 2026-06-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_player_groups"
down_revision: str | None = "0011_user_active"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OWNER_ROLE_NAME = "Owner"
_NEW_PERMISSIONS = ("group:read", "group:manage")


def upgrade() -> None:
    op.create_table(
        "player_group",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("community_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_player_group"),
        sa.ForeignKeyConstraint(
            ["community_id"],
            ["community.id"],
            name="fk_player_group_community_id_community",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "community_id", "kind", "name", name="uq_player_group_community_kind_name"
        ),
        sa.CheckConstraint(
            "kind IN ('op', 'whitelist')",
            name="ck_player_group_kind",
        ),
    )
    op.create_table(
        "group_player",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("player_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_group_player"),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["player_group.id"],
            name="fk_group_player_group_id_player_group",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "group_id", "player_uuid", name="uq_group_player_group_uuid"
        ),
    )
    op.create_table(
        "server_group",
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("group_id", "server_id", name="pk_server_group"),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["player_group.id"],
            name="fk_server_group_group_id_player_group",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["server.id"],
            name="fk_server_group_server_id_server",
            ondelete="CASCADE",
        ),
    )

    # Backfill the new codes onto existing preset Owner roles (mirrors 0008).
    for permission in _NEW_PERMISSIONS:
        op.execute(
            f"""
            UPDATE role
            SET permissions = array_append(permissions, '{permission}')
            WHERE is_preset = true
              AND name = '{_OWNER_ROLE_NAME}'
              AND NOT ('{permission}' = ANY(permissions))
            """
        )


def downgrade() -> None:
    for permission in _NEW_PERMISSIONS:
        op.execute(
            f"""
            UPDATE role
            SET permissions = array_remove(permissions, '{permission}')
            WHERE is_preset = true
              AND name = '{_OWNER_ROLE_NAME}'
              AND '{permission}' = ANY(permissions)
            """
        )
    op.drop_table("server_group")
    op.drop_table("group_player")
    op.drop_table("player_group")
