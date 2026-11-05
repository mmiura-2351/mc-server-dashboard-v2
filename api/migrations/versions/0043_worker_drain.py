"""fleet: persist the Workers an operator has drained

Issue #3535: drain intent lived only in the in-memory registry. It survived a
Worker reconnect but not an API restart, after which a decommissioning Worker
came back online and placeable.

``worker_drain`` holds one row per drained Worker id. ``PUT
/workers/{wid}/drain`` inserts it and ``DELETE`` removes it; the API loads the
set at startup so a Worker re-registering after a restart comes back draining.
There is no ``worker`` table (the registry stays in memory), so ``worker_id``
carries no foreign key.

No backfill: drains made before this migration were held only in the process
that is being replaced, so there is nothing to carry over.

Revision ID: 0043_worker_drain
Revises: 0042_server_community_restrict
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043_worker_drain"
down_revision: str | None = "0042_server_community_restrict"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_drain",
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("drained_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker_id", name="pk_worker_drain"),
    )


def downgrade() -> None:
    op.drop_table("worker_drain")
