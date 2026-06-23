"""server plugin side metadata column (issue #1308)

Adds the ``side`` column to ``server_plugin``: where the content is needed --
``server``, ``client``, or ``both`` (epic #650 item 5). Auto-detected at ingest
(Modrinth ``client_side`` / ``server_side``; Fabric ``environment``) and
manually overridable. ``side`` governs working-set presence: only a jar with
side in {``server``, ``both``} deploys to the running server; a ``client``-only
jar is tracked and cached but never placed in the working set.

The column is NOT NULL with a server default of ``'both'`` (the safe default --
a ``both`` jar is present everywhere), so rows installed before this migration
backfill to ``'both'``. A CHECK constraint mirrors the domain literal.

Revision ID: 0022_server_plugin_side
Revises: 0021_server_plugin_manifest
Create Date: 2026-06-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_server_plugin_side"
down_revision: str | None = "0021_server_plugin_manifest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "server_plugin",
        sa.Column(
            "side",
            sa.String(),
            nullable=False,
            server_default="both",
        ),
    )
    op.create_check_constraint(
        "ck_server_plugin_side",
        "server_plugin",
        "side IN ('server', 'client', 'both')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_server_plugin_side", "server_plugin", type_="check")
    op.drop_column("server_plugin", "side")
