"""server plugins: server_plugin table + plugin:read/plugin:manage permissions

Adds the ``server_plugin`` table (issue #1150): one row per installed
plugin/mod jar in a server's content directory. ``server_id`` FKs ``server``
(``ON DELETE CASCADE``); ``loader_type`` and ``source`` are CHECK-constrained
enums; ``(server_id, rel_path)`` is unique. ``installed_by`` is a soft
reference (no FK) so the row survives the actor's deletion.

Also backfills the new ``plugin:read`` and ``plugin:manage`` permission codes
onto every existing **preset** Owner role, mirroring 0008/0012/0017: the codes
were added to the catalog (the seed for *new* communities' Owner role), so
already-provisioned communities' Owner roles lack them and a data backfill
grants them. Only preset Owner rows (``is_preset = true`` AND ``name =
'Owner'``) are touched. Idempotent both ways.

Revision ID: 0019_server_plugins
Revises: 0018_resource_packs
Create Date: 2026-06-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_server_plugins"
down_revision: str | None = "0018_resource_packs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OWNER_ROLE_NAME = "Owner"
_NEW_PERMISSIONS = ("plugin:read", "plugin:manage")


def upgrade() -> None:
    op.create_table(
        "server_plugin",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rel_path", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("loader_type", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_project_id", sa.String(), nullable=True),
        sa.Column("source_version_id", sa.String(), nullable=True),
        sa.Column("version_number", sa.String(), nullable=True),
        sa.Column("checksum_sha512", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("installed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_server_plugin"),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["server.id"],
            name="fk_server_plugin_server_id_server",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "loader_type IN ('mod', 'plugin')",
            name="ck_server_plugin_loader_type",
        ),
        sa.CheckConstraint(
            "source IN ('local', 'modrinth')",
            name="ck_server_plugin_source",
        ),
        sa.UniqueConstraint(
            "server_id", "rel_path", name="uq_server_plugin_server_rel"
        ),
    )
    op.create_index(
        "ix_server_plugin_server_id",
        "server_plugin",
        ["server_id"],
    )

    # Backfill new codes onto existing preset Owner roles (mirrors 0008/0012/0017).
    for perm in _NEW_PERMISSIONS:
        op.execute(
            f"""
            UPDATE role
            SET permissions = array_append(permissions, '{perm}')
            WHERE is_preset = true
              AND name = '{_OWNER_ROLE_NAME}'
              AND NOT ('{perm}' = ANY(permissions))
            """
        )


def downgrade() -> None:
    for perm in _NEW_PERMISSIONS:
        op.execute(
            f"""
            UPDATE role
            SET permissions = array_remove(permissions, '{perm}')
            WHERE is_preset = true
              AND name = '{_OWNER_ROLE_NAME}'
              AND '{perm}' = ANY(permissions)
            """
        )
    op.drop_index("ix_server_plugin_server_id", table_name="server_plugin")
    op.drop_table("server_plugin")
