"""server: mark a server whose creation has yet to finish

Issue #4065: a create commits the server's row and then writes its working set,
the seed and for an import the archive, one write at a time. A creation that
fails or is interrupted part-way leaves the row with the working set as far as
it got, and a start hydrated that: an import missing its region files ran, and
Minecraft generated the chunks they held.

``creation_incomplete`` is set by the insert that creates the row and cleared
once the last write of the working set has returned, so a creation that fails,
is cancelled or dies before then leaves it set. While it is set, a start is
refused.

No backfill beyond the column default: every existing row reads ``false``,
startable as it was before this column.

Downgrade drops the column.

Revision ID: 0049_server_creation_incomplete
Revises: 0048_final_snapshot_refusal
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049_server_creation_incomplete"
down_revision: str | None = "0048_final_snapshot_refusal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "server",
        sa.Column(
            "creation_incomplete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("server", "creation_incomplete")
