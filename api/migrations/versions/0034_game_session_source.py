"""game_session: add nullable ``source`` discriminator

Adds the ``source`` column to ``game_session`` (issue #1912): the relay ingress
path a session was accepted on (``'java'`` / ``'bedrock'``), so the session
history can label a Bedrock flow-session honestly rather than as a Java
login-session whose claimed identity was unparseable.

The column is nullable with no default: existing rows keep NULL, which the read
surface maps to the legacy/unspecified source. New rows carry the value the
relay reports through ``SessionStart.source``; an older relay predating that
field reports unset, which the ingest seam also stores as NULL. Additive
nullable column, so it is not a breaking change.

Downgrade drops the column.

Revision ID: 0034_game_session_source
Revises: 0033_server_plugin_source_geyser
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_game_session_source"
down_revision: str | None = "0033_server_plugin_source_geyser"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("game_session", sa.Column("source", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("game_session", "source")
