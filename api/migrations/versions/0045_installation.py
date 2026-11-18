"""core: mint the database's installation id

Issue #3870: the JAR, plugin-cache and resource-pack GCs delete any stored object
no row of this database names. Pointed at a bucket that belongs to another
database, they deleted that database's objects. The API now records which
database owns its store in an owner marker, and the marker needs an id that names
this database.

``installation`` holds exactly one row: ``singleton`` is the primary key and the
CHECK admits only ``true``. The upgrade mints the id with ``gen_random_uuid()``;
nothing changes it afterwards, and a restore of the database carries it along.

A downgrade drops the id. Upgrading again mints a new one, which no longer
matches an existing marker, so the GCs refuse to run until an operator settles
which database owns the store (STORAGE.md Section 4.3).

Revision ID: 0045_installation
Revises: 0044_schedule_run_unknown
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0045_installation"
down_revision: str | None = "0044_schedule_run_unknown"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "installation",
        sa.Column("singleton", sa.Boolean(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("singleton", name="pk_installation"),
        sa.CheckConstraint("singleton", name="ck_installation_singleton"),
    )
    op.execute(
        "INSERT INTO installation (singleton, id, created_at) "
        "VALUES (true, gen_random_uuid(), now())"
    )


def downgrade() -> None:
    op.drop_table("installation")
