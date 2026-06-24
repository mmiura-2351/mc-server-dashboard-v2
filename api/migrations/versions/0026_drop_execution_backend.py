"""servers: drop ``execution_backend`` column

Container is the only backend; host_process was removed in issue #781.
This migration asserts no host_process rows exist (a safety check), drops the
``ck_server_execution_backend`` CHECK constraint, then drops the column itself.

Revision ID: 0026_drop_execution_backend
Revises: 0025_remove_server_type_spigot
Create Date: 2026-06-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision: str = "0026_drop_execution_backend"
down_revision: str | None = "0025_remove_server_type_spigot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_server_execution_backend"
_CHECK = "execution_backend IN ('host_process', 'container')"


def upgrade() -> None:
    conn = op.get_bind()
    count = conn.execute(
        text("SELECT count(*) FROM server WHERE execution_backend = 'host_process'")
    ).scalar()
    if count != 0:
        raise RuntimeError(
            f"Cannot drop execution_backend: {count} non-container rows exist"
        )
    op.drop_constraint(_CONSTRAINT, "server", type_="check")
    op.drop_column("server", "execution_backend")


def downgrade() -> None:
    op.add_column(
        "server",
        sa.Column(
            "execution_backend",
            sa.String(),
            nullable=False,
            server_default="container",
        ),
    )
    op.create_check_constraint(_CONSTRAINT, "server", _CHECK)
