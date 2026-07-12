"""servers: server.backup_retention

Adds the scheduled-backup retention policy to ``server`` (issue #1841). The
column is **nullable** jsonb: ``NULL`` means no retention is configured, so
scheduled backups accumulate unbounded (the pre-1841 behavior). A non-NULL
value is one of the two owner-confirmed policy forms — ``{"keep_last": N}``
(keep the newest N scheduled backups, N >= 1) or
``{"daily": D, "weekly": W, "monthly": M}`` (keep the newest scheduled backup
per UTC calendar day / ISO week / calendar month over those windows; each
tier >= 0, at least one > 0). The shape is validated in the domain
(``RetentionPolicy``), not by a CHECK constraint — mirroring the free-form
``config`` jsonb posture.

The policy applies only to ``source = scheduled`` backup rows; manual /
uploaded / event rows are never auto-pruned (DATABASE.md Section 7).

Revision ID: 0032_server_backup_retention
Revises: 0031_retire_backup_interval
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032_server_backup_retention"
down_revision: str | None = "0031_retire_backup_interval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "server",
        sa.Column("backup_retention", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("server", "backup_retention")
