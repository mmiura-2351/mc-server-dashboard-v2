"""servers: server.game_port

Adds the tracked Minecraft game port to ``server`` (issue #243). The column is
**nullable** so the existing rows (and any imported/legacy server) keep a NULL
port -- the create flow assigns a port to new servers, never to old ones -- and
carries a deployment-wide ``UNIQUE`` constraint. Under Postgres a UNIQUE
constraint treats NULLs as distinct, so any number of legacy rows may share the
NULL port while every assigned port is unique. The constraint is what backs the
"port already taken" 409 at create.

The column name and the ``uq_server_game_port`` constraint name follow the
metadata naming convention (issue #60), so the ORM model renders the same names
and an Alembic autogenerate stays quiet (the name-sync test guards this).

Revision ID: 0009_server_game_port
Revises: 0008_owner_audit_read_backfill
Create Date: 2026-06-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_server_game_port"
down_revision: str | None = "0008_owner_audit_read_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "server",
        sa.Column("game_port", sa.Integer(), nullable=True),
    )
    op.create_unique_constraint("uq_server_game_port", "server", ["game_port"])


def downgrade() -> None:
    op.drop_constraint("uq_server_game_port", "server", type_="unique")
    op.drop_column("server", "game_port")
