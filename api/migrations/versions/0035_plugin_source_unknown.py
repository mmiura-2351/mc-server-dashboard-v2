"""server_plugin: widen ``ck_server_plugin_source`` to ``unknown``

Issue #2059 adds a fourth ``PluginSource`` value, ``unknown``, for a jar
re-ingested at backup restore whose catalog origin no checksum match could
recover: the ghost re-ingestion path records it as ``source='unknown'`` instead
of asserting ``local`` (a manual upload), so the loss of provenance is marked
honestly rather than hidden. The live CHECK, widened to three values by 0033,
must admit the new value or that INSERT would violate ``ck_server_plugin_source``.
This migration recreates the CHECK with the four values the model now renders.

The constraint keeps the SAME explicit ``ck_server_plugin_source`` name (pinned on
the model), so an Alembic autogenerate sees no rename. Recreating a CHECK is a
drop + add: the new clause matches the model's rendering exactly (``IN`` list in
model order, single-quoted values), mirroring the 0033 widening it extends.

Downgrade restores the 0033 three-value CHECK. Any ``unknown`` rows present at
downgrade would violate that narrower CHECK, so the downgrade first remaps them to
``local`` -- the value the ghost path asserted before this change, and a downgrade
that rejected data valid at head would itself be a latent migration bug.

Revision ID: 0035_plugin_source_unknown
Revises: 0034_game_session_source
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0035_plugin_source_unknown"
down_revision: str | None = "0034_game_session_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_server_plugin_source"
_NEW_CHECK = "source IN ('local', 'modrinth', 'geyser', 'unknown')"
_OLD_CHECK = "source IN ('local', 'modrinth', 'geyser')"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "server_plugin", type_="check")
    op.create_check_constraint(_CONSTRAINT, "server_plugin", _NEW_CHECK)


def downgrade() -> None:
    # Remap rows that only the widened CHECK admits before re-narrowing it, so the
    # ALTER never rejects data that was valid at head.
    op.execute("UPDATE server_plugin SET source = 'local' WHERE source = 'unknown'")
    op.drop_constraint(_CONSTRAINT, "server_plugin", type_="check")
    op.create_check_constraint(_CONSTRAINT, "server_plugin", _OLD_CHECK)
