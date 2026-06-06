"""identity: refresh_token.revoked_reason

Records *why* a refresh token was revoked so the reuse grace window can grace
only rotation-revoked predecessors (issue #369). Keying the grace solely on
``revoked_at`` recency also graced tokens revoked by a family revoke (theft
response), letting an attacker re-presenting a just-revoked successor inside the
window escape the family revoke. The cause column closes that hole: the grace
branch requires ``revoked_reason = 'rotated'``; family- and logout-revoked
tokens stay on the theft path regardless of recency.

The column is nullable -- null whenever ``revoked_at`` is null (the token is not
revoked). Existing already-revoked rows backfill null, so they are *not*
graceable, which is the safe default (a re-presented legacy revoked token is
treated as theft, never re-issued).

Revision ID: 0014_refresh_token_reason
Revises: 0013_backup_source_uploaded
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_refresh_token_reason"
down_revision: str | None = "0013_backup_source_uploaded"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "refresh_token",
        sa.Column("revoked_reason", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("refresh_token", "revoked_reason")
