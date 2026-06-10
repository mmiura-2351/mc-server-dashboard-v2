"""server: add the ``store_generation`` column

Issue #763 records the authoritative working-set generation per server: a
monotonically increasing counter Storage bumps on each ``commit_snapshot`` and
the snapshot data plane persists here. The reconciler compares it against the
generation a Worker reports holding to decide whether a same-worker restart must
hydrate (a Worker holding a STALE generation must hydrate), generalizing the
presence-only hydrate-skip of #698 into "presence at a fresh-enough generation".

The column is NOT NULL with a server-side default of 0, mirroring the style of the
``game_port`` / ``health`` additions. Adding it NOT NULL with that default
backfills every pre-existing row to 0 in the same statement -- the honest state
for rows with no snapshot accounted for yet, matching the Worker's "nothing held"
default so the reconciler's ``worker-gen < store-gen`` comparison treats both
consistently. It is ``BigInteger`` so the counter never overflows in practice.

Downgrade drops the column.

Numbering note (issue #763): authored as ``0016`` off head ``0015``. If another
migration merges first, this renumbers to main's head at the final rebase
(CONTRIBUTING.md Section 3) -- a trivial ``down_revision`` bump.

Revision ID: 0016_server_store_generation
Revises: 0015_backup_health
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_server_store_generation"
down_revision: str | None = "0015_backup_health"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "server",
        sa.Column(
            "store_generation",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("server", "store_generation")
