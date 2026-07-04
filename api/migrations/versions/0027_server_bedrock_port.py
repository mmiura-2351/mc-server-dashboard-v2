"""servers: server.bedrock_port

Adds the tracked public Bedrock UDP port to ``server`` (issue #1541). The column
is **nullable**: ``NULL`` means the server is not Bedrock-enabled (no Geyser
plugin detected) -- non-NULL *is* the Bedrock-enabled state, there is no separate
boolean. The port is allocated from the dedicated UDP window when Geyser arrives
through the normal plugin paths and released (set back to NULL) on Geyser
uninstall; a server delete drops the row and with it the port. The
deployment-wide ``UNIQUE`` constraint mirrors ``uq_server_game_port`` (migration
0009): Postgres treats NULLs as distinct, so any number of non-Bedrock rows
coexist while every allocated port is unique.

The column name and the ``uq_server_bedrock_port`` constraint name follow the
metadata naming convention (issue #60), so the ORM model renders the same names
and an Alembic autogenerate stays quiet (the name-sync test guards this).

Revision ID: 0027_server_bedrock_port
Revises: 0026_drop_execution_backend
Create Date: 2026-07-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_server_bedrock_port"
down_revision: str | None = "0026_drop_execution_backend"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "server",
        sa.Column("bedrock_port", sa.Integer(), nullable=True),
    )
    op.create_unique_constraint("uq_server_bedrock_port", "server", ["bedrock_port"])


def downgrade() -> None:
    op.drop_constraint("uq_server_bedrock_port", "server", type_="unique")
    op.drop_column("server", "bedrock_port")
