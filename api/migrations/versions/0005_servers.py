"""servers: server

Creates the ``server`` table exactly per docs/app/DATABASE.md Section 7: the
authoritative record of a Minecraft server, community-scoped (FK ``ON DELETE
CASCADE``), with the desired/observed state split (FR-SRV-4) as CHECK-constrained
enum columns, the immutable-by-policy ``execution_backend`` (FR-EXE-3) as a CHECK
enum, the ``config`` JSONB blob, and a nullable ``assigned_worker_id``. The latter
is a plain UUID with no FK: the ``worker`` table is not yet a persisted relation
(the fleet registry is in-memory), so DATABASE.md's ``ON DELETE SET NULL`` FK to
``worker.id`` lands when that table does. ``UNIQUE(community_id, name)`` enforces
per-community name uniqueness; the ``(assigned_worker_id)`` index backs the "all
servers on Worker X" lookup (FR-WRK-4).

Revision ID: 0005_servers
Revises: 0004_community
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_servers"
down_revision: str | None = "0004_community"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "server",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("community_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("mc_edition", sa.String(), nullable=False),
        sa.Column("mc_version", sa.String(), nullable=False),
        sa.Column("server_type", sa.String(), nullable=False),
        sa.Column("execution_backend", sa.String(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("desired_state", sa.String(), nullable=False),
        sa.Column("observed_state", sa.String(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        # No FK: the worker table is not yet persisted (DATABASE.md Section 7 note).
        sa.Column("assigned_worker_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_server"),
        sa.ForeignKeyConstraint(
            ["community_id"],
            ["community.id"],
            name="fk_server_community_id_community",
            ondelete="CASCADE",
        ),
        # A server name is unique within its community (DATABASE.md Section 7).
        sa.UniqueConstraint("community_id", "name", name="uq_server_community_name"),
        sa.CheckConstraint(
            "server_type IN ('vanilla', 'paper', 'forge')",
            name="ck_server_type",
        ),
        sa.CheckConstraint(
            "execution_backend IN ('host_process', 'container')",
            name="ck_server_execution_backend",
        ),
        sa.CheckConstraint(
            "desired_state IN ('running', 'stopped')",
            name="ck_server_desired_state",
        ),
        sa.CheckConstraint(
            "observed_state IN ('starting', 'running', 'stopping', 'stopped', "
            "'restarting', 'crashed', 'unknown')",
            name="ck_server_observed_state",
        ),
    )
    # Index on (assigned_worker_id) for "all servers on Worker X" (FR-WRK-4).
    op.create_index(
        "ix_server_assigned_worker_id",
        "server",
        ["assigned_worker_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_server_assigned_worker_id", table_name="server")
    op.drop_table("server")
