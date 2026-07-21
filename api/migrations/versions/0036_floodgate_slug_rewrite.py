"""server_plugin: rewrite ``source_project_id`` for existing Floodgate installs

PR #2113 renamed the GeyserMC catalog's Floodgate project id from ``floodgate``
to ``geysermc-floodgate`` but shipped no data migration. Every pre-#2113
Floodgate install stored ``source_project_id='floodgate'`` with
``source='geyser'``, so update checks now route to Modrinth instead of
GeyserMC -- silently reporting "up to date" forever (issue #2145).

This data-only migration rewrites the stored project id for GeyserMC-sourced
rows. Modrinth-sourced ``floodgate`` rows (Fabric servers) are intentionally
untouched: their ``source='modrinth'`` is correct.

Downgrade restores the old slug for rows this migration touched.

Revision ID: 0036_floodgate_slug_rewrite
Revises: 0035_plugin_source_unknown
Create Date: 2026-07-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0036_floodgate_slug_rewrite"
down_revision: str | None = "0035_plugin_source_unknown"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_SLUG = "floodgate"
_NEW_SLUG = "geysermc-floodgate"


def upgrade() -> None:
    op.execute(
        f"UPDATE server_plugin SET source_project_id = '{_NEW_SLUG}' "
        f"WHERE source_project_id = '{_OLD_SLUG}' AND source = 'geyser'"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE server_plugin SET source_project_id = '{_OLD_SLUG}' "
        f"WHERE source_project_id = '{_NEW_SLUG}' AND source = 'geyser'"
    )
