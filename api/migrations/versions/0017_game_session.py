"""relay ingress: game_session table + session:read permission

Adds the ``game_session`` table (RELAY.md Section 14, issue #957): one row per
accepted login session recorded by the relay, keyed on the relay-minted ``id``
(the ``ReportSessions`` idempotency key). ``player_ip`` is a PostgreSQL ``INET``;
``server_id`` FKs ``server`` (``ON DELETE CASCADE``) and is indexed with
``started_at`` for the newest-first listing.

``server_id`` / ``hostname`` / ``player_ip`` / ``started_at`` are nullable so a
``SessionEnd`` that arrives before its ``SessionStart`` (an out-of-order batch
retry) can create a placeholder row carrying only ``id`` + ``ended_at`` (the
start fills the rest in). ``username`` / ``player_uuid`` are the *claimed* Login
Start values (RELAY.md Section 8) and may be ``NULL``; ``ended_at`` is ``NULL``
while the session is open.

Also backfills the new ``session:read`` permission code onto every existing
**preset** Owner role, mirroring 0008/0012: the code was added to the catalog
(the seed for *new* communities' Owner role), so already-provisioned communities'
Owner roles lack it and a data backfill grants it. Only preset Owner rows
(``is_preset = true`` AND ``name = 'Owner'``) are touched. Idempotent both ways.

Revision ID: 0017_game_session
Revises: 0016_relay_ingress
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_game_session"
down_revision: str | None = "0016_relay_ingress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OWNER_ROLE_NAME = "Owner"
_NEW_PERMISSION = "session:read"


def upgrade() -> None:
    op.create_table(
        "game_session",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("hostname", sa.String(), nullable=True),
        sa.Column("player_ip", postgresql.INET(), nullable=True),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("player_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_game_session"),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["server.id"],
            name="fk_game_session_server_id_server",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_game_session_server_id_started_at",
        "game_session",
        ["server_id", "started_at"],
    )

    # Backfill the new code onto existing preset Owner roles (mirrors 0008/0012).
    op.execute(
        f"""
        UPDATE role
        SET permissions = array_append(permissions, '{_NEW_PERMISSION}')
        WHERE is_preset = true
          AND name = '{_OWNER_ROLE_NAME}'
          AND NOT ('{_NEW_PERMISSION}' = ANY(permissions))
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE role
        SET permissions = array_remove(permissions, '{_NEW_PERMISSION}')
        WHERE is_preset = true
          AND name = '{_OWNER_ROLE_NAME}'
          AND '{_NEW_PERMISSION}' = ANY(permissions)
        """
    )
    op.drop_index("ix_game_session_server_id_started_at", table_name="game_session")
    op.drop_table("game_session")
