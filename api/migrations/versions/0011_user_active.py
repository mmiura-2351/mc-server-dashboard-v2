"""identity: user.active

Adds the account-lifecycle flag to ``user`` (issue #278). A deactivated account
keeps its row -- so audit history and the username/email uniqueness survive --
but cannot authenticate. The column is ``NOT NULL`` with a server default of the
SQL literal ``true`` so every existing row backfills active (no account is
silently locked out by the upgrade).

The column name follows the metadata naming convention (issue #60), so the ORM
model renders the same name and an Alembic autogenerate stays quiet (the
name-sync test guards this).

Revision ID: 0011_user_active
Revises: 0010_server_type_fabric
Create Date: 2026-06-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_user_active"
down_revision: str | None = "0010_server_type_fabric"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("user", "active")
