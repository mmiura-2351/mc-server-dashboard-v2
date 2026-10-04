"""refresh_token: group each sign-in session's tokens into a rotation chain

Issue #3249: logout revoked only the presented refresh token. A refresh rotated
server-side whose response was still in flight left its successor valid, and the
late ``Set-Cookie`` could install it over the next user's cookie in the same
browser.

This migration adds ``chain_id``: login starts a chain and every rotation's
successor inherits it, so logout can revoke the whole sign-in session, successors
included, without touching the user's other sessions. Indexed for that revoke.

Existing rows predate the chain, and the schema records no rotation lineage, so a
rotated predecessor and its live successor cannot be told apart from two
separate sign-ins. Giving each row its own chain would split a rotation that
spans the upgrade, and a logout with the predecessor would leave the successor
valid. Instead each user's pre-upgrade tokens share one *legacy chain*, derived
from the user id: a logout of any legacy session revokes all of that user's
legacy sessions. That fails closed. Rotations inherit the legacy chain, so the
coupling lasts until the user's legacy sessions end and they sign in afresh;
sessions started after the upgrade get chains of their own.

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
    op.execute(
        "UPDATE refresh_token "
        "SET chain_id = md5('mcsd-legacy-chain:' || user_id::text)::uuid"
    )
    op.alter_column("refresh_token", "chain_id", nullable=False)
    op.create_index(_INDEX, "refresh_token", ["chain_id"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="refresh_token")
    op.drop_column("refresh_token", "chain_id")
