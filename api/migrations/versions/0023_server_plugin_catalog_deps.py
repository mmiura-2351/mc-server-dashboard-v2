"""server plugin catalog dependencies column (issue #1321)

Adds the ``catalog_dependencies`` JSON column to ``server_plugin``: the
**required** Modrinth catalog dependencies of a Modrinth-sourced plugin, keyed
by ``project_id`` (epic #650). Many popular mods (e.g. Roughly Enough Items)
declare their dependencies only in their Modrinth project metadata, not in the
jar manifest's ``depends``; the manifest-driven phase-B validation and phase-C
resolution never see those, so a freshly-installed REI reported "No issues" even
though Architectury / Cloth Config were missing.

The column stores the selected version's required catalog deps as
``[{"project_id": str, "required": bool, "slug": str | None, "title": str |
None}]`` -- the ``slug`` / ``title`` carried at ingest so the WebUI can render a
readable label without an extra Modrinth round-trip. Validation matches a dep's
``project_id`` against installed plugins' ``source_project_id``; resolution
seeds the closure frontier with the unsatisfied ones.

Nullable: rows installed before this migration keep NULL until re-ingested (the
repository maps a NULL JSON column to an empty list), and local-upload jars
leave it null/empty. No backfill -- a re-install picks the metadata up.

Revision ID: 0023_server_plugin_catalog_deps
Revises: 0022_server_plugin_side
Create Date: 2026-06-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0023_server_plugin_catalog_deps"
down_revision: str | None = "0022_server_plugin_side"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "server_plugin",
        sa.Column("catalog_dependencies", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("server_plugin", "catalog_dependencies")
