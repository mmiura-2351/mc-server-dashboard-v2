"""server: record that the assigned Worker refused a stop's final snapshot

Issue #4054 (part of #3697, owner decision on #3701): a stop whose final
snapshot the Worker refuses ``BUSY`` or ``UNAVAILABLE`` is to keep its Worker
until the snapshot succeeds or an operator releases it. Nothing on the row
told such a hold from a final snapshot still in flight, so neither a client nor
the release could know why the server is held.

``final_snapshot_refusal`` is the refusal: ``busy`` or ``unavailable``. It is
written only while the row is ``desired_state=stopped`` and still assigned to
the Worker that answered, set to NULL again by a later answer that is not a
refusal, and set to NULL with the assignment by every release. Its CHECK
(``ck_server_final_snapshot_refusal``, pinned on the model) admits the two
refusals.

No backfill: no stop was held by a refusal before the column, so every row
reads NULL.

Downgrade drops the column, its CHECK with it.

Revision ID: 0048_final_snapshot_refusal
Revises: 0047_server_awaiting_hydrate
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048_final_snapshot_refusal"
down_revision: str | None = "0047_server_awaiting_hydrate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "server", sa.Column("final_snapshot_refusal", sa.String(), nullable=True)
    )
    op.create_check_constraint(
        "ck_server_final_snapshot_refusal",
        "server",
        "final_snapshot_refusal IN ('busy', 'unavailable')",
    )


def downgrade() -> None:
    op.drop_column("server", "final_snapshot_refusal")
