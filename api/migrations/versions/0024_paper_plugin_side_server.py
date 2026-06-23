"""Force side='server' for existing Paper plugins (issue #1342)

Data-only migration: existing Paper plugins were stored with side='both'
(the default), but Paper/Bukkit plugins are always server-side only. This
corrects the stored value to match the new backend enforcement.

Revision ID: 0024_paper_plugin_side_server
Revises: 0023_server_plugin_catalog_deps
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024_paper_plugin_side_server"
down_revision: str | None = "0023_server_plugin_catalog_deps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE server_plugin SET side = 'server'
        WHERE server_id IN (SELECT id FROM server WHERE server_type = 'paper')
        AND side != 'server'
        """
    )


def downgrade() -> None:
    # Data-only: no structural rollback. The old default was 'both', but
    # reverting blindly would be incorrect for plugins that were genuinely
    # server-side before the migration. Leave the data as-is.
    pass
