"""servers: record the player-group file regenerations a server is owed

Issue #3223: an OP / whitelist group change made while a server runs cannot be
written to the server's authoritative ``ops.json`` / ``whitelist.json`` -- the
Worker's live working set, and the final snapshot taken from it at stop, would
overwrite it -- and nothing recorded that the write was owed, so it was never
made. A removed operator kept level 4 across every restart.

``server_group_sync_pending`` holds one row per ``(server, kind)`` whose file a
group change has made stale. The change records it in its own transaction and
the server's next start regenerates the file and clears the row. ``token``
changes each time the row is recorded again, so a start clears only the mark it
read. Rows go with their server (``ON DELETE CASCADE``).

No backfill: which running servers missed a change before this migration is not
recorded anywhere. Editing a group, or re-attaching it, marks its servers.

Revision ID: 0041_server_group_sync_pending
Revises: 0040_refresh_token_chain
Create Date: 2026-10-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041_server_group_sync_pending"
down_revision: str | None = "0040_refresh_token_chain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "server_group_sync_pending",
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "server_id", "kind", name="pk_server_group_sync_pending"
        ),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["server.id"],
            name="fk_server_group_sync_pending_server_id_server",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "kind IN ('op', 'whitelist')",
            name="ck_server_group_sync_pending_kind",
        ),
    )


def downgrade() -> None:
    op.drop_table("server_group_sync_pending")
