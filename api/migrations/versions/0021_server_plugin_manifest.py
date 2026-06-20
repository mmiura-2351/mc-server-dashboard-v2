"""server plugin manifest metadata columns (issue #1307)

Adds the jar manifest metadata columns to ``server_plugin``: the uniform
dependency source parsed at ingest for both local uploads and Modrinth installs
(epic #650 phase B). ``mod_identifier`` is the manifest's declared id; ``provides``
are alias ids the jar also satisfies; ``dependencies`` carry
``[{mod_identifier, version_range, required, conflict}]``; ``mc_versions`` are the
declared compatible Minecraft versions.

All nullable: rows installed before this migration carry NULL until re-ingested,
and the repository maps a NULL JSON column to an empty list. The phase-B
validator reads these to surface missing required deps, conflicts, and
MC-version mismatch.

Revision ID: 0021_server_plugin_manifest
Revises: 0020_server_plugin_sha256
Create Date: 2026-06-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_server_plugin_manifest"
down_revision: str | None = "0020_server_plugin_sha256"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "server_plugin",
        sa.Column("mod_identifier", sa.String(), nullable=True),
    )
    op.add_column(
        "server_plugin",
        sa.Column("provides", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "server_plugin",
        sa.Column("dependencies", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "server_plugin",
        sa.Column("mc_versions", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("server_plugin", "mc_versions")
    op.drop_column("server_plugin", "dependencies")
    op.drop_column("server_plugin", "provides")
    op.drop_column("server_plugin", "mod_identifier")
