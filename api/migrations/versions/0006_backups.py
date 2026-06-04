"""backups: backup

Creates the ``backup`` table exactly per docs/app/DATABASE.md Section 8: the
retained-snapshot *metadata* for a server (FR-BAK-1), pointing at the archive bytes
that live behind the ``Storage`` Port by an opaque ``storage_ref``. The
``server_id`` FK is ``ON DELETE CASCADE`` (a server's backups go with it); the
``source`` column is the CHECK-constrained ``manual`` / ``scheduled`` / ``event``
enum; ``created_by`` is a plain nullable UUID with **no FK** — a soft reference so
the row survives the actor's deletion (the audit trail is the durable actor record,
Section 9), and a scheduled backup with no actor stores NULL. The
``(server_id, created_at)`` index backs listing a server's backups newest-first.

Revision ID: 0006_backups
Revises: 0005_servers
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_backups"
down_revision: str | None = "0005_servers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backup",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("storage_ref", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        # No FK: a soft reference so the row survives the actor's deletion
        # (DATABASE.md Section 8/9).
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_backup"),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["server.id"],
            name="fk_backup_server_id_server",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "source IN ('manual', 'scheduled', 'event')",
            name="ck_backup_source",
        ),
    )
    # Index on (server_id, created_at) for listing a server's backups newest-first.
    op.create_index(
        "ix_backup_server_id_created_at",
        "backup",
        ["server_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_backup_server_id_created_at", table_name="backup")
    op.drop_table("backup")
