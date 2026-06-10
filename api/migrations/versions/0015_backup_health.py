"""backups: add the ``health`` status column

Issue #742 records each backup's structural health so an operator never restores
corruption unknowingly. A backup created through the integrity-gated create path
(#749) is ``healthy`` by construction; legacy and uploaded rows that predate any
check are ``unknown`` until the one-shot sweep (#744) classifies them; a row a
check found corrupt is ``quarantined``.

The column is NOT NULL with a server-side default of ``unknown``, mirroring the
``source`` column's CHECK-constrained-string style (migration 0013). Adding it
NOT NULL with that default backfills every pre-existing row to ``unknown`` in the
same statement -- the honest state for rows that predate the health check. A
``ck_backup_health`` CHECK pins the three allowed values, with the SAME explicit
name the model renders so the name-sync guard stays quiet and an autogenerate
sees no rename.

Downgrade drops the CHECK then the column.

Numbering note (issue #742): authored as ``0015`` off head ``0014``. If another
migration merges first, this renumbers to main's head at the final rebase
(CONTRIBUTING.md Section 3) -- a trivial ``down_revision`` bump.

Revision ID: 0015_backup_health
Revises: 0014_refresh_token_reason
Create Date: 2026-06-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_backup_health"
down_revision: str | None = "0014_refresh_token_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_backup_health"
_CHECK = "health IN ('healthy', 'quarantined', 'unknown')"


def upgrade() -> None:
    op.add_column(
        "backup",
        sa.Column(
            "health",
            sa.String(),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.create_check_constraint(_CONSTRAINT, "backup", _CHECK)


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "backup", type_="check")
    op.drop_column("backup", "health")
