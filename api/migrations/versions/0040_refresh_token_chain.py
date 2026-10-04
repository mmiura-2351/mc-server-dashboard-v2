"""refresh_token: group each sign-in session's tokens into a rotation chain

Issue #3249: logout revoked only the presented refresh token. A refresh rotated
server-side whose response was still in flight left its successor valid, and the
late ``Set-Cookie`` could install it over the next user's cookie in the same
browser.

This migration adds ``chain_id``: login starts a chain and every rotation's
successor inherits it, so logout can revoke the whole sign-in session, successors
included, without touching the user's other sessions. Indexed for that revoke.

Existing rows predate the chain, so each is backfilled as its own chain
(``chain_id = id``): a session that rotates after the upgrade carries its current
token's chain forward.

Downgrade drops the index and the column.

Revision ID: 0040_refresh_token_chain
Revises: 0039_resource_grant_parent_fks
Create Date: 2026-10-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040_refresh_token_chain"
down_revision: str | None = "0039_resource_grant_parent_fks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_refresh_token_chain_id"


def upgrade() -> None:
    op.add_column(
        "refresh_token",
        sa.Column("chain_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute("UPDATE refresh_token SET chain_id = id")
    op.alter_column("refresh_token", "chain_id", nullable=False)
    op.create_index(_INDEX, "refresh_token", ["chain_id"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="refresh_token")
    op.drop_column("refresh_token", "chain_id")
