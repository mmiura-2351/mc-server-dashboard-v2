"""servers: narrow ``ck_server_type`` to drop spigot

The 0010 migration widened the CHECK to five values including ``spigot``. Since
spigot was never actually supported (create always rejected it), no rows carry
the value. This migration narrows the CHECK back to the four supported types
(vanilla, paper, fabric, forge) and asserts no spigot rows exist.

Revision ID: 0025_remove_server_type_spigot
Revises: 0024_paper_plugin_side_server
Create Date: 2026-06-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0025_remove_server_type_spigot"
down_revision: str | None = "0024_paper_plugin_side_server"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_server_type"
_NEW_CHECK = "server_type IN ('vanilla', 'paper', 'fabric', 'forge')"
_OLD_CHECK = "server_type IN ('vanilla', 'paper', 'fabric', 'forge', 'spigot')"


def upgrade() -> None:
    conn = op.get_bind()
    count = conn.execute(
        text("SELECT count(*) FROM server WHERE server_type = 'spigot'")
    ).scalar()
    assert count == 0, f"cannot narrow CHECK: {count} spigot row(s) exist"
    op.drop_constraint(_CONSTRAINT, "server", type_="check")
    op.create_check_constraint(_CONSTRAINT, "server", _NEW_CHECK)


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "server", type_="check")
    op.create_check_constraint(_CONSTRAINT, "server", _OLD_CHECK)
