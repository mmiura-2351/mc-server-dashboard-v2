"""server_mods: server↔mod assignment table

Adds the ``server_mods`` table for many-to-many server↔mod assignment (issue
#1262, epic #1258). One row per (server, mod) pair: a server's mod set. The
deployed jar is physically placed into the server's working set by the assignment
use cases; this row only indexes the link. ``server_id`` FKs ``server``
(``ON DELETE CASCADE``); ``mod_id`` FKs ``mods``; ``assigned_by`` is a plain UUID
(no FK) so the row survives the actor's deletion. ``UNIQUE(server_id, mod_id)``
enforces one assignment per pair; an index on ``server_id`` backs listing a
server's mod set.

Revision ID: 0020_server_mods
Revises: 0019_mods
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_server_mods"
down_revision: str | None = "0019_mods"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "server_mods",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mod_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("assigned_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_server_mods"),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["server.id"],
            name="fk_server_mods_server_id_server",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mod_id"],
            ["mods.id"],
            name="fk_server_mods_mod_id_mods",
        ),
        sa.UniqueConstraint(
            "server_id", "mod_id", name="uq_server_mods_server_id_mod_id"
        ),
    )
    op.create_index("ix_server_mods_server_id", "server_mods", ["server_id"])


def downgrade() -> None:
    op.drop_index("ix_server_mods_server_id", table_name="server_mods")
    op.drop_table("server_mods")
