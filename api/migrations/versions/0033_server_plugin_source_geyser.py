"""server_plugin: widen ``ck_server_plugin_source`` to ``geyser``

The 0019 migration pinned the ``server_plugin`` table's ``source`` CHECK to
``('local', 'modrinth')``. Issue #1905 adds GeyserMC's download API as a catalog
source for Floodgate-Spigot (which Modrinth does not carry for Paper), recording
a catalog install from it as a new plugin row with ``source='geyser'`` -- so the
live CHECK must be widened or that INSERT would violate ``ck_server_plugin_source``.
This migration recreates the CHECK with the three values the model now renders.

The constraint keeps the SAME explicit ``ck_server_plugin_source`` name (pinned on
the model), so an Alembic autogenerate sees no rename. Recreating a CHECK is a
drop + add: the new clause matches the model's rendering exactly (``IN`` list in
model order, single-quoted values), mirroring the 0013 ``ck_backup_source``
widening.

Downgrade restores the 0019 two-value CHECK. Any ``geyser`` rows present at
downgrade would violate that narrower CHECK, so the downgrade first remaps them to
``modrinth`` -- a downgrade that rejected data valid at head would itself be a
latent migration bug.

Revision ID: 0033_server_plugin_source_geyser
Revises: 0032_server_backup_retention
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0033_server_plugin_source_geyser"
down_revision: str | None = "0032_server_backup_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_server_plugin_source"
_NEW_CHECK = "source IN ('local', 'modrinth', 'geyser')"
_OLD_CHECK = "source IN ('local', 'modrinth')"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "server_plugin", type_="check")
    op.create_check_constraint(_CONSTRAINT, "server_plugin", _NEW_CHECK)


def downgrade() -> None:
    # Remap rows that only the widened CHECK admits before re-narrowing it, so the
    # ALTER never rejects data that was valid at head.
    op.execute("UPDATE server_plugin SET source = 'modrinth' WHERE source = 'geyser'")
    op.drop_constraint(_CONSTRAINT, "server_plugin", type_="check")
    op.create_check_constraint(_CONSTRAINT, "server_plugin", _OLD_CHECK)
