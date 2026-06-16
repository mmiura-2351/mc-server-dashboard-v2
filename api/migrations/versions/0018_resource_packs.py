"""resource packs: resource_packs + server_resource_pack_assignments tables

Adds two tables for global resource pack management (issue #1175):

``resource_packs``: global resource pack metadata (one row per uploaded pack).
``uploaded_by`` is a plain UUID (no FK) so the row survives the actor's
deletion.

``server_resource_pack_assignments``: per-server assignment linking a server
to a resource pack (at most one assignment per server, ``server_id`` is PK).
``server_id`` FKs ``server`` (``ON DELETE CASCADE``); ``resource_pack_id``
FKs ``resource_packs``. ``assigned_by`` is a plain UUID (no FK).

Revision ID: 0018_resource_packs
Revises: 0017_game_session
Create Date: 2026-06-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_resource_packs"
down_revision: str | None = "0017_game_session"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resource_packs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("sha1_hash", sa.String(40), nullable=False),
        sa.Column("sha256_hash", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_resource_packs"),
    )

    op.create_table(
        "server_resource_pack_assignments",
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_pack_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "require_resource_pack",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column("resource_pack_prompt", sa.String(), nullable=True),
        sa.Column("assigned_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "server_id", name="pk_server_resource_pack_assignments"
        ),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["server.id"],
            name="fk_server_resource_pack_assignments_server_id_server",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resource_pack_id"],
            ["resource_packs.id"],
            name="fk_server_resource_pack_assignments_resource_pack_id_resource_packs",
        ),
    )


def downgrade() -> None:
    op.drop_table("server_resource_pack_assignments")
    op.drop_table("resource_packs")
