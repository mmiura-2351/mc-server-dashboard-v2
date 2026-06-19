"""mods: global mod library table

Adds the ``mods`` table for the global mod library (issue #1259, epic #1258).
One row per uploaded jar; mods are global (not community-scoped) and the jar
bytes live in the top-level ``mods/<mod-id>/`` object-store namespace.
``uploaded_by`` is a plain UUID (no FK) so the row survives the actor's deletion.
CHECK constraints fence the enum-like ``loader_type`` / ``side`` / ``source``
columns; a unique index on ``sha256_hash`` enforces content-address dedup.

Revision ID: 0019_mods
Revises: 0018_resource_packs
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_mods"
down_revision: str | None = "0018_resource_packs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mods",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("loader_type", sa.String(), nullable=False),
        sa.Column("mod_identifier", sa.String(), nullable=False),
        sa.Column("provides", postgresql.JSONB(), nullable=False),
        sa.Column("version_number", sa.String(), nullable=False),
        sa.Column("mc_versions", postgresql.JSONB(), nullable=False),
        sa.Column("side", sa.String(), nullable=False, server_default="both"),
        sa.Column("dependencies", postgresql.JSONB(), nullable=False),
        sa.Column("sha256_hash", sa.String(64), nullable=False),
        sa.Column("sha512_hash", sa.String(128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_project_id", sa.String(), nullable=True),
        sa.Column("source_version_id", sa.String(), nullable=True),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_mods"),
        sa.CheckConstraint(
            "loader_type IN ('fabric', 'forge', 'neoforge', 'quilt', 'paper')",
            name="ck_mods_loader_type",
        ),
        sa.CheckConstraint(
            "side IN ('server', 'client', 'both')",
            name="ck_mods_side",
        ),
        sa.CheckConstraint(
            "source IN ('local', 'modrinth')",
            name="ck_mods_source",
        ),
    )
    # Content-address dedup: an identical jar resolves to the existing entry.
    op.create_index("uq_mods_sha256_hash", "mods", ["sha256_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_mods_sha256_hash", table_name="mods")
    op.drop_table("mods")
