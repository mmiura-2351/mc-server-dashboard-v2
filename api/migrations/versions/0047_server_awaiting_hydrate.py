"""server: mark an assignment whose Worker has not hydrated the server yet

Issue #3890: a start keeps its Worker when that Worker refuses the hydrate with
an answer that leaves an instance of the server there possible. The row then
names a Worker whose scratch may be a leftover older than the store, and the
data plane accepted a stale-base publish from the assigned Worker (issue #1703),
so a snapshot of that scratch could replace the store's newer working set.

``awaiting_hydrate`` is set by the write that places the server on a Worker for
a start and cleared once that Worker holds the store's working set: its hydrate
succeeded, or its held generation is at least the store's. It is cleared too
when the Worker reports the server ``running``, and by every release, which
clears it with the assignment. While it is set, no snapshot is taken from that
Worker and the data plane refuses a stale-base publish from it.

No backfill beyond the column default: every existing row reads ``false``,
which is what the API assumed of an assigned Worker before this column.

Downgrade drops the column.

Revision ID: 0047_server_awaiting_hydrate
Revises: 0046_server_last_crash
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_server_awaiting_hydrate"
down_revision: str | None = "0046_server_last_crash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "server",
        sa.Column(
            "awaiting_hydrate",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("server", "awaiting_hydrate")
