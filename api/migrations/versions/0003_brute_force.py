"""brute-force: login_attempt and account_lockout tables

Creates the two auth-hardening tables per docs/app/SECURITY.md Section 3, kept
separate from the core entity model: ``login_attempt`` is the append-only record
the per-username/per-IP sliding windows COUNT over (with the matching
``(username, created_at)`` and ``(ip, created_at)`` indexes), and
``account_lockout`` holds at most one row per username with the active lockout
and the historic count that drives the exponential back-off.

Revision ID: 0003_brute_force
Revises: 0002_identity
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_brute_force"
down_revision: str | None = "0002_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_attempt",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("ip", sa.String(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("failure_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_login_attempt"),
    )
    # Per-username sliding-window COUNT (SECURITY.md Section 3).
    op.create_index(
        "ix_login_attempt_username_created_at",
        "login_attempt",
        ["username", "created_at"],
    )
    # Per-IP sliding-window COUNT (SECURITY.md Section 3).
    op.create_index(
        "ix_login_attempt_ip_created_at",
        "login_attempt",
        ["ip", "created_at"],
    )

    op.create_table(
        "account_lockout",
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "lockout_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("username", name="pk_account_lockout"),
    )


def downgrade() -> None:
    op.drop_table("account_lockout")
    op.drop_index("ix_login_attempt_ip_created_at", table_name="login_attempt")
    op.drop_index("ix_login_attempt_username_created_at", table_name="login_attempt")
    op.drop_table("login_attempt")
